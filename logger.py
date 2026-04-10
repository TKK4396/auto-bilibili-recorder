"""
日志工具模块 - 同时输出到 stdout 和文件

功能：
1. 同时输出日志到 stdout（docker logs）和文件（便于宿主机查看）
2. 从 /etc/hostname 读取容器标识名
3. 按日期自动清理旧日志（默认保留30天）
"""

import os
import sys
import time
from datetime import datetime, timedelta


class DualLogger:
    """双写日志器：同时输出到 stdout 和文件"""
    
    def __init__(self, log_dir: str = "/storage", keep_days: int = 30):
        self.log_dir = log_dir
        self.keep_days = keep_days
        self.hostname = self._get_hostname()
        self.current_date = datetime.now().strftime("%Y%m%d")
        self.log_file = os.path.join(log_dir, f"{self.hostname}_{self.current_date}.log")
        self._file = None
        self._init_logger()
        self._cleanup_old_logs()
    
    def _get_hostname(self) -> str:
        """从 /etc/hostname 读取主机名"""
        try:
            with open('/etc/hostname', 'r') as f:
                hostname = f.read().strip()
                print(f"[LOGGER] 读取到主机名: {hostname}")
                return hostname
        except Exception as e:
            print(f"[LOGGER] 读取主机名失败，使用默认值: {e}", file=sys.__stderr__)
            return "unknown"
    
    def _init_logger(self):
        """初始化日志文件"""
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            self._file = open(self.log_file, 'a', encoding='utf-8')
            self.write(f"[LOGGER] 日志系统初始化完成")
            self.write(f"[LOGGER] 日志文件: {self.log_file}")
        except Exception as e:
            print(f"[LOGGER] 初始化日志文件失败: {e}", file=sys.__stderr__)
            self._file = None
    
    def _cleanup_old_logs(self):
        """清理超过保留天数的日志"""
        try:
            cutoff = datetime.now() - timedelta(days=self.keep_days)
            prefix = f"{self.hostname}_"
            cleaned_count = 0
            
            for filename in os.listdir(self.log_dir):
                if filename.startswith(prefix) and filename.endswith('.log'):
                    date_str = filename.replace(prefix, '').replace('.log', '')
                    try:
                        file_date = datetime.strptime(date_str, "%Y%m%d")
                        if file_date < cutoff:
                            file_path = os.path.join(self.log_dir, filename)
                            os.remove(file_path)
                            cleaned_count += 1
                    except ValueError:
                        # 日期格式不正确，跳过
                        pass
            
            if cleaned_count > 0:
                self.write(f"[LOGGER] 清理了 {cleaned_count} 个过期日志文件（保留 {self.keep_days} 天）")
            else:
                self.write(f"[LOGGER] 无过期日志需要清理")
                
        except Exception as e:
            print(f"[LOGGER] 清理日志失败: {e}", file=sys.__stderr__)
    
    def write(self, text: str):
        """写入日志"""
        if not text.endswith('\n'):
            text += '\n'
        
        # 写入文件
        if self._file:
            try:
                self._file.write(text)
                self._file.flush()
            except Exception as e:
                print(f"[LOGGER] 写入日志文件失败: {e}", file=sys.__stderr__)
        
        # 输出到 stdout（保持 docker logs 正常）
        sys.__stdout__.write(text)
        sys.__stdout__.flush()
    
    def flush(self):
        """刷新缓冲区"""
        if self._file:
            self._file.flush()
        sys.__stdout__.flush()


def setup_logging(log_dir: str = "/storage", keep_days: int = 30) -> DualLogger:
    """
    设置日志系统
    
    Args:
        log_dir: 日志文件目录
        keep_days: 日志保留天数（默认30天）
    
    Returns:
        DualLogger 实例
    """
    logger = DualLogger(log_dir, keep_days)
    
    # 重定向 stdout 和 stderr
    sys.stdout = logger
    sys.stderr = logger
    
    return logger
