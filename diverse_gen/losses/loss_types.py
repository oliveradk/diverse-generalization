from enum import Enum 

class LossType(Enum):
    EXP = 'exp'
    PROB = 'prob'
    TOPK = 'topk'
    SMOOTH = 'smooth'
    CONF = 'conf'
    DIVDIS = 'divdis'
    DBAT = 'dbat'
    ERM = 'erm'