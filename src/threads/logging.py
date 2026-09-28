from aws_publisher import AWSPublisher

import queue

class Logging:
    def __init__(self, filepath: str, queue: queue.Queue):

        #Intitialize the TinyDB Handler
        #self.db_handler = DBHandler(filepath)

        self.queue = queue
        #self.cloud.publish(event)

    #def task_logging(self):
        