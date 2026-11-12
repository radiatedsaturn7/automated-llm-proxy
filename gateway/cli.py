import argparse
import os
from copy import deepcopy

import uvicorn
from uvicorn.config import LOGGING_CONFIG


def logging_config():
    config = deepcopy(LOGGING_CONFIG)
    config['formatters']['metadata'] = {'format': '%(message)s'}
    config['handlers']['metadata'] = {'class': 'logging.StreamHandler', 'formatter': 'metadata', 'stream': 'ext://sys.stdout'}
    config['loggers']['gateway.http'] = {'handlers': ['metadata'], 'level': 'INFO', 'propagate': False}
    return config


def shutdown_grace(value):
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("Shutdown grace must be an integer from 1 to 3600 seconds") from None
    if not 1 <= seconds <= 3600:
        raise argparse.ArgumentTypeError("Shutdown grace must be an integer from 1 to 3600 seconds")
    return seconds


def main():
    parser = argparse.ArgumentParser(description='Brrrtgate gateway')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--shutdown-grace-seconds', type=shutdown_grace,
                        default=os.environ.get('SHUTDOWN_GRACE_SECONDS', '360'),
                        help='Request drain window after SIGTERM (env SHUTDOWN_GRACE_SECONDS; default 360)')
    args = parser.parse_args()
    uvicorn.run('gateway.main:app', host=args.host, port=args.port, proxy_headers=False,
                access_log=False, log_config=logging_config(),
                timeout_graceful_shutdown=args.shutdown_grace_seconds)


if __name__ == '__main__':
    main()
