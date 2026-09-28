from aws_publisher import AWSPublisher
from tinydb_handler import DB_Handler

import queue

class Logging:
    def __init__(self, filepath: str, queue: queue.Queue):

        self.db_handler = DB_Handler(filepath)

        self.queue = queue
        #self.cloud.publish(event)
        