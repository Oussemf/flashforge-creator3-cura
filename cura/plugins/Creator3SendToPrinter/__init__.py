from .Plugin import SendToPrinter


def getMetaData():
    return {}


def register(app):
    return {"extension": SendToPrinter(app)}
