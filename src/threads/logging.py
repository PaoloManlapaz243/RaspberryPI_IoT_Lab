from aws_publisher import AWSPublisher
from db_handler import DBHandler

import queue

class Logging:
    def __init__(self, filepath: str, queue: queue.Queue):

        self.db_handler = DBHandler(filepath)

        self.queue = queue
        #self.cloud.publish(event)
        