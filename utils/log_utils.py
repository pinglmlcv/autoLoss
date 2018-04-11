import logging

def get_logger(name=__file__, level=logging.INFO):
    logger = logging.getLogger(name)

    if getattr(logger, '_init_done__', None):
        logger.setLevel(level)
        return logger
    logger._init_done__ = True
    logger.propagate = False
    logger.setLevel(level)

    formatter = logging.Formatter("%(levelname)s::%(message)s")
    # formatter = logging.Formatter("%(asctime)s:%(filename)s:%(levelname)s::%(message)s")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.setLevel(0)

    del logger.handlers[:]
    logger.addHandler(handler)

    return logger

logger = get_logger()
