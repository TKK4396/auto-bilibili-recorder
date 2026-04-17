import pymysql

class DBManager:
    def __init__(self, host, user, password, database):
        self.db_config = {
            'host': host,
            'user': user,
            'password': password,
            'database': database,
            'cursorclass': pymysql.cursors.DictCursor,
            'autocommit': True
        }

    def get_connection(self):
        return pymysql.connect(**self.db_config)

    def insert_task(self, task_data):
        sql = """
            INSERT INTO upload_task_record
            (session_id, room_id, video_path, thumbnail_path, sc_path, he_path, subtitle_path, title, source, description, tag, channel_id, danmaku, account_name, status, extra_info)
            VALUES (%(session_id)s, %(room_id)s, %(video_path)s, %(thumbnail_path)s, %(sc_path)s, %(he_path)s, %(subtitle_path)s, %(title)s, %(source)s, %(description)s, %(tag)s, %(channel_id)s, %(danmaku)s, %(account_name)s, 0, %(extra_info)s)
        """
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, task_data)
                return cursor.lastrowid

    def update_status(self, task_id, status, error_msg=""):
        sql = "UPDATE upload_task_record SET status = %s, error_msg = %s WHERE id = %s"
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, (status, error_msg, task_id))

    def get_tasks_by_status(self, status):
        sql = "SELECT * FROM upload_task_record WHERE status = %s"
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, (status,))
                return cursor.fetchall()

    def get_all_tasks(self, limit=100):
        sql = "SELECT * FROM upload_task_record ORDER BY create_time DESC LIMIT %s"
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, (limit,))
                return cursor.fetchall()

    def get_task_by_id(self, task_id):
        sql = "SELECT * FROM upload_task_record WHERE id = %s"
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, (task_id,))
                return cursor.fetchone()

    def get_non_success_tasks(self, limit=100):
        sql = "SELECT * FROM upload_task_record WHERE status != 2 ORDER BY create_time DESC LIMIT %s"
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, (limit,))
                return cursor.fetchall()
