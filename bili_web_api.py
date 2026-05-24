import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import urllib
from dataclasses import dataclass, asdict, field, InitVar
from json import JSONDecodeError
from os.path import splitext, basename
from typing import Union, Any
from urllib import parse
from urllib.parse import quote

import aiohttp
import requests
import requests.utils
import rsa
import xml.etree.ElementTree as ET
from requests.adapters import HTTPAdapter, Retry

logger = logging.getLogger(__name__)


class BiliBili:
    def __init__(self, video: 'Data'):
        self.app_key = None
        self.appsec = None
        if self.app_key is None or self.appsec is None:
            self.app_key = 'ae57252b0c09105d'
            self.appsec = 'c75875c596a69eb55bd119e74b07cfe3'
        self.__session = requests.Session()
        self.video = video
        self.__session.mount('https://', HTTPAdapter(max_retries=Retry(total=5)))
        self.__session.headers.update({
            'user-agent': "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/63.0.3239.108",
            'referer': "https://www.bilibili.com/",
            'connection': 'keep-alive'
        })
        self.cookies = None
        self.access_token = None
        self.refresh_token = None
        self.account = None
        self.__bili_jct = None
        self._auto_os = None
        self.persistence_path = 'engine/bili.cookie'

    @property
    def bili_jct(self):
        return self.__bili_jct

    def set_cookies(self, cookie_dict):
        """直接注入 cookie dict，不依赖文件路径。
        兼容嵌套格式 {cookie_info: {cookies: [...]}} 和扁平格式 {name: value}"""
        self.cookies = cookie_dict
        self.login_by_cookies(cookie_dict)

    def get_web_qrcode(self):
        """生成 B站 Web 端扫码登录二维码"""
        response = self.__session.post(
            'https://passport.bilibili.com/x/passport-login/web/qrcode/generate',
            timeout=5
        )
        return response.json()

    def poll_web_qrcode_once(self, qrcode_key):
        """单次轮询 Web 端扫码状态（不循环），成功后提取 cookies"""
        response = self.__session.get(
            'https://passport.bilibili.com/x/passport-login/web/qrcode/poll',
            params={'qrcode_key': qrcode_key},
            timeout=5
        )
        r = response.json()

        if r and r.get('code') == 0:
            data = r.get('data', {})
            cookie_info = data.get('cookie_info', {})
            for cookie in cookie_info.get('cookies', []):
                self.__session.cookies.set(cookie['name'], cookie['value'])
                if cookie['name'] == 'bili_jct':
                    self.__bili_jct = cookie['value']
            redirect_url = data.get('url', '')
            if redirect_url:
                try:
                    self.__session.get(redirect_url, timeout=5, allow_redirects=True)
                except Exception:
                    pass
            self.cookies = self.__session.cookies.get_dict()
            # 提取 token_info
            token_info = data.get('token_info', {})
            if token_info:
                self.access_token = token_info.get('access_token')
                self.refresh_token = token_info.get('refresh_token')

        return r

    def check_tag(self, tag):
        r = self.__session.get("https://member.bilibili.com/x/vupre/web/topic/tag/check?tag=" + tag).json()
        if r["code"] == 0:
            return True
        else:
            return False

    def get_qrcode(self):
        params = {
            "appkey": "4409e2ce8ffd12b8",
            "local_id": "0",
            "ts": int(time.time()),
        }
        params["sign"] = hashlib.md5(
            f"{urllib.parse.urlencode(params)}59b43e04ad6965f34319062b478f83dd".encode()).hexdigest()
        response = self.__session.post("http://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code", data=params,
                                       timeout=5)
        r = response.json()
        if r and r["code"] == 0:
            return r

    async def login_by_qrcode(self, value):
        params = {
            "appkey": "4409e2ce8ffd12b8",
            "auth_code": value["data"]["auth_code"],
            "local_id": "0",
            "ts": int(time.time()),
        }
        params["sign"] = hashlib.md5(
            f"{urllib.parse.urlencode(params)}59b43e04ad6965f34319062b478f83dd".encode()).hexdigest()
        for i in range(0, 120):
            await asyncio.sleep(1)
            response = self.__session.post("http://passport.bilibili.com/x/passport-tv-login/qrcode/poll", data=params,
                                           timeout=5)
            r = response.json()
            if r and r["code"] == 0:
                return r
        raise Exception("Qrcode timeout")

    def tid_archive(self, cookies):
        requests.utils.add_dict_to_cookiejar(self.__session.cookies, cookies)
        response = self.__session.get("https://member.bilibili.com/x/vupre/web/archive/pre")
        return response.json()

    def myinfo(self, cookies):
        requests.utils.add_dict_to_cookiejar(self.__session.cookies, cookies)
        response = self.__session.get('http://api.bilibili.com/x/space/myinfo')
        return response.json()

    def login(self, persistence_path, user_cookie):
        self.persistence_path = user_cookie
        if os.path.isfile(self.persistence_path):
            print('使用持久化内容上传')
            self.load()
        if self.cookies:
            try:
                self.login_by_cookies(self.cookies)
            except Exception:
                logger.exception('login error')
                self.login_by_password(**self.account)
        else:
            self.login_by_password(**self.account)
        self.store()

    def load(self):
        try:
            with open(self.persistence_path) as f:
                self.cookies = json.load(f)
                # 兼容旧扁平格式：{access_token, refresh_token, ...cookies}
                if 'token_info' in self.cookies:
                    self.access_token = self.cookies['token_info']['access_token']
                    self.refresh_token = self.cookies['token_info']['refresh_token']
                elif 'access_token' in self.cookies:
                    logger.info('检测到旧版扁平 cookie 格式，自动迁移')
                    self.access_token = self.cookies.pop('access_token', None)
                    self.refresh_token = self.cookies.pop('refresh_token', None)
                    self.cookies = {
                        'cookie_info': {
                            'cookies': [
                                {'name': k, 'value': v}
                                for k, v in self.cookies.items()
                            ]
                        },
                        'token_info': {
                            'access_token': self.access_token,
                            'refresh_token': self.refresh_token
                        }
                    }
        except (JSONDecodeError, KeyError):
            logger.exception('加载cookie出错')

    def store(self):
        with open(self.persistence_path, "w") as f:
            json.dump(self.cookies, f)

    def send_sms(self, phone_number, country_code):
        params = {
            "actionKey": "appkey",
            "appkey": "783bbb7264451d82",
            "build": 6510400,
            "channel": "bili",
            "cid": country_code,
            "device": "phone",
            "mobi_app": "android",
            "platform": "android",
            "tel": phone_number,
            "ts": int(time.time()),
        }
        sign = hashlib.md5(f"{urllib.parse.urlencode(params)}2653583c8873dea268ab9386918b1d65".encode()).hexdigest()
        payload = f"{urllib.parse.urlencode(params)}&sign={sign}"
        response = self.__session.post("https://passport.bilibili.com/x/passport-login/sms/send", data=payload,
                                       timeout=5)
        return response.json()

    def login_by_sms(self, code, params):
        params["code"] = code
        params["sign"] = hashlib.md5(
            f"{urllib.parse.urlencode(params)}59b43e04ad6965f34319062b478f83dd".encode()).hexdigest()
        response = self.__session.post("https://passport.bilibili.com/x/passport-login/login/sms", data=params,
                                       timeout=5)
        r = response.json()
        if r and r.get('code') == 0:
            try:
                for cookie in r['data']['cookie_info']['cookies']:
                    self.__session.cookies.set(cookie['name'], cookie['value'])
                    if 'bili_jct' == cookie['name']:
                        self.__bili_jct = cookie['value']
                self.cookies = self.__session.cookies.get_dict()
                self.access_token = r['data']['token_info']['access_token']
                self.refresh_token = r['data']['token_info']['refresh_token']
            except Exception:
                pass
            return r

    def login_by_password(self, username, password):
        print('使用账号上传')
        key_hash, pub_key = self.get_key()
        encrypt_password = base64.b64encode(rsa.encrypt(f'{key_hash}{password}'.encode(), pub_key)).decode()
        payload = {
            "actionKey": 'appkey',
            "appkey": self.app_key,
            "build": 6270200,
            "captcha": '',
            "challenge": '',
            "channel": 'bili',
            "device": 'phone',
            "mobi_app": 'android',
            "password": encrypt_password,
            "permission": 'ALL',
            "platform": 'android',
            "seccode": "",
            "subid": 1,
            "ts": int(time.time()),
            "username": username,
            "validate": "",
        }
        response = self.__session.post("https://passport.bilibili.com/x/passport-login/oauth2/login", timeout=5,
                                       data={**payload, 'sign': self.sign(parse.urlencode(payload))})
        r = response.json()
        if r['code'] != 0 or r.get('data') is None or r['data'].get('cookie_info') is None:
            raise RuntimeError(r)
        try:
            for cookie in r['data']['cookie_info']['cookies']:
                self.__session.cookies.set(cookie['name'], cookie['value'])
                if 'bili_jct' == cookie['name']:
                    self.__bili_jct = cookie['value']
            self.cookies = self.__session.cookies.get_dict()
            self.access_token = r['data']['token_info']['access_token']
            self.refresh_token = r['data']['token_info']['refresh_token']
        except Exception:
            raise RuntimeError(r)
        return r

    def login_by_cookies(self, cookie):
        logger.info(f'{self.__class__.__name__}: login by cookies')
        # 兼容两种 cookie 格式：嵌套 (含 cookie_info) 和扁平 dict
        if isinstance(cookie, dict) and 'cookie_info' in cookie:
            cookies_dict = {c['name']: c['value'] for c in cookie['cookie_info']['cookies']}
        else:
            cookies_dict = cookie
        requests.utils.add_dict_to_cookiejar(self.__session.cookies, cookies_dict)
        if 'bili_jct' in cookies_dict:
            self.__bili_jct = cookies_dict['bili_jct']
        data = self.__session.get("https://api.bilibili.com/x/web-interface/nav", timeout=5).json()
        if data["code"] != 0:
            raise Exception(data)
        print('使用cookies上传')

    def sign(self, param):
        return hashlib.md5(f"{param}{self.appsec}".encode()).hexdigest()

    def get_key(self):
        url = "https://passport.bilibili.com/x/passport-login/web/key"
        payload = {
            'appkey': f'{self.app_key}',
            'sign': self.sign(f"appkey={self.app_key}"),
        }
        response = self.__session.get(url, data=payload, timeout=5)
        r = response.json()
        if r and r["code"] == 0:
            return r['data']['hash'], rsa.PublicKey.load_pkcs1_openssl_pem(r['data']['key'].encode())

    def probe(self):
        ret = self.__session.get('https://member.bilibili.com/preupload?r=probe', timeout=5).json()
        logger.info(f"线路:{ret['lines']}")
        data, auto_os = None, None
        min_cost = 0
        if ret['probe'].get('get'):
            method = 'get'
        else:
            method = 'post'
            data = bytes(int(1024 * 0.1 * 1024))
        for line in ret['lines']:
            start = time.perf_counter()
            test = self.__session.request(method, f"https:{line['probe_url']}", data=data, timeout=30)
            cost = time.perf_counter() - start
            print(line['query'], cost)
            if test.status_code != 200:
                return
            if not min_cost or min_cost > cost:
                auto_os = line
                min_cost = cost
        auto_os['cost'] = min_cost
        return auto_os

    def upload_file(self, filepath: str, lines='AUTO', tasks=3):
        """上传本地视频文件,返回视频信息dict
        b站目前支持 upos 上传线路
        """
        preferred_upos_cdn = None
        if not self._auto_os:
            if lines == 'bda':
                self._auto_os = {"os": "upos", "query": "upcdn=bda&probe_version=20221109",
                                 "probe_url": "//upos-cs-upcdnbda.bilivideo.com/OK"}
                preferred_upos_cdn = 'bda'
            elif lines in {'bda2', 'cs-bda2'}:
                self._auto_os = {"os": "upos", "query": "upcdn=bda2&probe_version=20221109",
                                 "probe_url": "//upos-cs-upcdnbda2.bilivideo.com/OK"}
                preferred_upos_cdn = 'bda2'
            elif lines == 'ws':
                self._auto_os = {"os": "upos", "query": "upcdn=ws&probe_version=20221109",
                                 "probe_url": "//upos-cs-upcdnws.bilivideo.com/OK"}
                preferred_upos_cdn = 'ws'
            elif lines in {'qn', 'cs-qn'}:
                self._auto_os = {"os": "upos", "query": "upcdn=qn&probe_version=20221109",
                                 "probe_url": "//upos-cs-upcdnqn.bilivideo.com/OK"}
                preferred_upos_cdn = 'qn'
            elif lines == 'bldsa':
                self._auto_os = {"os": "upos", "query": "upcdn=bldsa&probe_version=20221109",
                                 "probe_url": "//upos-cs-upcdnbldsa.bilivideo.com/OK"}
                preferred_upos_cdn = 'bldsa'
            elif lines == 'tx':
                self._auto_os = {"os": "upos", "query": "upcdn=tx&probe_version=20221109",
                                 "probe_url": "//upos-cs-upcdntx.bilivideo.com/OK"}
                preferred_upos_cdn = 'tx'
            elif lines == 'txa':
                self._auto_os = {"os": "upos", "query": "upcdn=txa&probe_version=20221109",
                                 "probe_url": "//upos-cs-upcdntxa.bilivideo.com/OK"}
                preferred_upos_cdn = 'txa'
            elif lines in ('kodo', 'cos', 'cos-internal'):
                logger.warning(f"线路 '{lines}' 已废弃，改用自动探测")
                self._auto_os = self.probe()
            else:
                self._auto_os = self.probe()
            logger.info(f"线路选择 => {self._auto_os['os']}: {self._auto_os['query']}. time: {self._auto_os.get('cost')}")
        if self._auto_os['os'] == 'upos':
            upload = self.upos
        else:
            logger.error(f"NoSearch:{self._auto_os['os']}")
            raise NotImplementedError(self._auto_os['os'])
        logger.info(f"os: {self._auto_os['os']}")
        total_size = os.path.getsize(filepath)
        with open(filepath, 'rb') as f:
            query = {
                'r': self._auto_os['os'],
                'profile': 'ugcupos/bup',
                'ssl': 0,
                'version': '2.8.12',
                'build': 2081200,
                'name': f.name,
                'size': total_size,
            }
            resp = self.__session.get(
                f"https://member.bilibili.com/preupload?{self._auto_os['query']}", params=query,
                timeout=5)
            ret = resp.json()
            logger.debug(f"preupload: {ret}")
            if preferred_upos_cdn:
                original_endpoint = ret['endpoint']
                if re.match(r'//upos-(sz|cs)-upcdn(bda2|ws|qn)\.bilivideo\.com', original_endpoint):
                    if re.match(r'bda2|qn|ws', preferred_upos_cdn):
                        logger.debug(f"Preferred UpOS CDN: {preferred_upos_cdn}")
                        new_endpoint = re.sub(r'upcdn(bda2|qn|ws)', f'upcdn{preferred_upos_cdn}', original_endpoint)
                        logger.debug(f"{original_endpoint} => {new_endpoint}")
                        ret['endpoint'] = new_endpoint
                    else:
                        logger.error(f"Unrecognized preferred_upos_cdn: {preferred_upos_cdn}")
                else:
                    logger.warning(f"Assigned UpOS endpoint {original_endpoint} was never seen before, "
                                   f"so will not modify it")
            return asyncio.run(upload(f, total_size, ret, tasks=tasks))

    async def upos(self, file, total_size, ret, tasks=3):
        filename = file.name
        chunk_size = ret['chunk_size']
        auth = ret["auth"]
        endpoint = ret["endpoint"]
        biz_id = ret["biz_id"]
        upos_uri = ret["upos_uri"]
        url = f"https:{endpoint}/{upos_uri.replace('upos://', '')}"
        headers = {
            "X-Upos-Auth": auth
        }
        upload_id = self.__session.post(f'{url}?uploads&output=json', timeout=15,
                                        headers=headers).json()["upload_id"]
        parts = []
        chunks = math.ceil(total_size / chunk_size)

        async def upload_chunk(session, chunks_data, params):
            async with session.put(url, params=params, raise_for_status=True,
                                   data=chunks_data, headers=headers):
                end = time.perf_counter() - start
                parts.append({"partNumber": params['chunk'] + 1, "eTag": "etag"})
                sys.stdout.write(f"\r{params['end'] / 1000 / 1000 / end:.2f}MB/s "
                                 f"=> {params['partNumber'] / chunks:.1%}")

        start = time.perf_counter()
        await self._upload({
            'uploadId': upload_id,
            'chunks': chunks,
            'total': total_size
        }, file, chunk_size, upload_chunk, tasks=tasks)
        cost = time.perf_counter() - start
        p = {
            'name': filename,
            'uploadId': upload_id,
            'biz_id': biz_id,
            'output': 'json',
            'profile': 'ugcupos/bup'
        }
        attempt = 0
        while attempt <= 5:
            try:
                r = self.__session.post(url, params=p, json={"parts": parts}, headers=headers, timeout=15).json()
                if r.get('OK') == 1:
                    logger.info(f'{filename} uploaded >> {total_size / 1000 / 1000 / cost:.2f}MB/s. {r}')
                    return {"title": splitext(os.path.basename(filename))[0],
                            "filename": splitext(basename(upos_uri))[0], "desc": ""}
                raise IOError(r)
            except IOError:
                attempt += 1
                logger.info(f"请求合并分片时出现问题，尝试重连，次数：" + str(attempt))
                time.sleep(15)

    @staticmethod
    async def _upload(params, file, chunk_size, afunc, tasks=3):
        params['chunk'] = -1

        async def upload_chunk():
            while True:
                chunks_data = file.read(chunk_size)
                if not chunks_data:
                    return
                params['chunk'] += 1
                params['size'] = len(chunks_data)
                params['partNumber'] = params['chunk'] + 1
                params['start'] = params['chunk'] * chunk_size
                params['end'] = params['start'] + params['size']
                clone = params.copy()
                for i in range(10):
                    try:
                        await afunc(session, chunks_data, clone)
                        break
                    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                        logger.error(f"retry chunk{clone['chunk']} >> {i + 1}. {e}")

        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*[upload_chunk() for _ in range(tasks)])

    def submit(self, submit_api=None):
        if not self.video.title:
            self.video.title = self.video.videos[0]["title"]
        self.__session.get('https://member.bilibili.com/x/geetest/pre/add', timeout=5)

        if submit_api is None:
            total_info = self.__session.get('http://api.bilibili.com/x/space/myinfo', timeout=15).json()
            if total_info.get('data') is None:
                logger.error(total_info)
            total_info = total_info.get('data')
            if total_info and total_info['level'] > 3 and total_info['follower'] > 1000:
                user_weight = 2
            else:
                user_weight = 1
            logger.info(f'用户权重: {user_weight}')
            submit_api = 'web'

        ret = None
        if submit_api == 'web':
            ret = self.submit_web()
            if ret["code"] != 0:
                logger.error(f'网页端接口提交失败: {ret}')
                raise Exception(ret)
        if not ret:
            raise Exception(f'不存在的选项：{submit_api}')
        return ret

    def submit_web(self):
        logger.info('使用网页端api提交')
        return self.__session.post(f'https://member.bilibili.com/x/vu/web/add?csrf={self.__bili_jct}', timeout=5,
                                   json=asdict(self.video)).json()

    def fetch_edit_data(self, bvid: str):
        """获取现有视频的编辑数据，用于追加分P"""
        resp = self.__session.get(
            f'https://member.bilibili.com/x/vu/web/edit?bvid={bvid}',
            timeout=15
        )
        r = resp.json()
        if r.get('code') != 0:
            raise RuntimeError(f"获取编辑数据失败: {r}")
        return r

    def edit_submit(self, bvid: str):
        """提交编辑（追加分P、修改元信息），需要 self.video 已填充完整数据"""
        if not self.__bili_jct:
            raise RuntimeError("bili_jct (CSRF token) 缺失，请先登录")
        logger.info('使用网页端api提交编辑')
        resp = self.__session.post(
            f'https://member.bilibili.com/x/vu/web/edit?csrf={self.__bili_jct}',
            json=asdict(self.video),
            timeout=15
        )
        ret = resp.json()
        if ret.get('code') != 0:
            raise RuntimeError(f"编辑提交失败: {ret}")
        return ret

    def cover_up(self, img: str):
        """
        :param img: img path or stream
        :return: img URL
        """
        from PIL import Image
        from io import BytesIO

        with Image.open(img) as im:
            xsize, ysize = im.size
            if xsize / ysize > 1.6:
                delta = xsize - ysize * 1.6
                region = im.crop((int(delta / 2), 0, int(xsize - delta / 2), ysize))
            else:
                delta = ysize - xsize * 10 / 16
                region = im.crop((0, int(delta / 2), xsize, int(ysize - delta / 2)))
            buffered = BytesIO()
            region.save(buffered, format=im.format)
        r = self.__session.post(
            url='https://member.bilibili.com/x/vu/web/cover/up',
            data={
                'cover': b'data:image/jpeg;base64,' + (base64.b64encode(buffered.getvalue())),
                'csrf': self.__bili_jct
            }, timeout=30
        )
        buffered.close()
        res = r.json()
        if res.get('data') is None:
            raise Exception(res)
        return res['data']['url']

    def get_tags(self, upvideo, typeid="", desc="", cover="", groupid=1, vfea=""):
        """
        上传视频后获得推荐标签
        :param vfea:
        :param groupid:
        :param typeid:
        :param desc:
        :param cover:
        :param upvideo:
        :return: 返回官方推荐的tag
        """
        url = f'https://member.bilibili.com/x/web/archive/tags?' \
              f'typeid={typeid}&title={quote(upvideo["title"])}&filename=filename&desc={desc}&cover={cover}' \
              f'&groupid={groupid}&vfea={vfea}'
        return self.__session.get(url=url, timeout=5).json()

    def __enter__(self):
        return self

    def __exit__(self, e_t, e_v, t_b):
        self.close()

    def close(self):
        """Closes all adapters and as such the session"""
        self.__session.close()


@dataclass
class Data:
    """
    cover: 封面图片，可由recovers方法得到视频的帧截图
    """
    copyright: int = 2
    source: str = ''
    tid: int = 21
    cover: str = ''
    title: str = ''
    desc_format_id: int = 0
    desc: str = ''
    dynamic: str = ''
    subtitle: dict = field(init=False)
    tag: Union[list, str] = ''
    videos: list = field(default_factory=list)
    dtime: Any = None
    open_subtitle: InitVar[bool] = False

    def __post_init__(self, open_subtitle):
        self.subtitle = {"open": int(open_subtitle), "lan": ""}
        if self.dtime and self.dtime - int(time.time()) <= 14400:
            self.dtime = None
        if isinstance(self.tag, list):
            self.tag = ','.join(self.tag)

    def delay_time(self, dtime: int):
        """设置延时发布时间，距离提交大于2小时，格式为10位时间戳"""
        if dtime - int(time.time()) > 7200:
            self.dtime = dtime

    def set_tag(self, tag: list):
        """设置标签，tag为数组"""
        self.tag = ','.join(tag)

    def append(self, video):
        self.videos.append(video)
