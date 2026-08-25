import os


class FaultPoint:

    def __init__(self):
        self.active_fault = None
        

    def set_fault(self, name: str):
        self.active_fault = name

    def check(self,name: str):
        if self.active_fault == name:
            os._exit(1)

#Global fault point instance
faults = FaultPoint()