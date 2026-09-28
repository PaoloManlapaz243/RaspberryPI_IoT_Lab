from tinydb import TinyDB


# class DB_Queue_Object:
#     def __init__(self, queue_obj:dict[str, str]):

class DB_Handler:
    def __init__(self, filepath: str):
        self.db = TinyDB(filepath)

    def insert_queue2db(self, logs: list[dict[str, str]]):
        self.db.insert_multiple(logs)
