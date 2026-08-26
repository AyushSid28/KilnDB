import os
#A class through which we can inject faults at specific points in our code
class FaultPoint:

    def __init__(self):
        #When kiln starts active fault is none, means no fault is injected at start
        self.active_fault = None
        

    def set_fault(self, name: str):
        #this basically tells that a fault of type name is injected at start of this method
        #So kiln knows when we will reach this point I have to die/crash
        self.active_fault = name

    #This method checks if any fault is injected at this point
    def check(self,name: str):
        #This basically checks if active fault is equal to the listed fault name
        if self.active_fault == name:
            #After this Os will exit and kiln will never reach the end of the method
            os._exit(1)

#Global fault point instance
faults = FaultPoint()


