import argparse
from copy import deepcopy

import uvicorn
from uvicorn.config import LOGGING_CONFIG


def logging_config():
    config = deepcopy(LOGGING_CONFIG)
    config['formatters']['metadata'] = {'format': '%(message)s'}
    config['handlers']['metadata'] = {'class': 'logging.StreamHandler', 'formatter': 'metadata', 'stream': 'ext://sys.stdout'}
    config['loggers']['gateway.http'] = {'handlers': ['metadata'], 'level': 'INFO', 'propagate': False}
    return config


def main():
    parser = argparse.ArgumentParser(description='Brrrtgate gateway')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run('gateway.main:app', host=args.host, port=args.port, proxy_headers=False,
                access_log=False, log_config=logging_config())


if __name__ == '__main__':
    main()
