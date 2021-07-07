# source: https://gist.github.com/pteichman/84b92ae7cef0ab98f5a8

import threading  # :(

class WaitGroup(object):
    """WaitGroup is like Go sync.WaitGroup.
    
    Without all the useful corner cases.
    """
    def __init__(self):
        self.count = 0
        self.cv = threading.Condition()

    def add(self, n):
        self.cv.acquire()
        self.count += n
        self.cv.release()

    def done(self):
        self.cv.acquire()
        self.count -= 1
        if self.count == 0:
            self.cv.notify_all()
        self.cv.release()

    def clear(self):
        self.cv.acquire()
        self.count = 0
        self.cv.notify_all()
        self.cv.release()

    def wait(self):
        self.cv.acquire()
        while self.count > 0:
            self.cv.wait()
        self.cv.release()
