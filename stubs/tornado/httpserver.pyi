from typing import Optional, Union, Dict, Any, ByteString
from tornado import web
import ssl

class HTTPServer:
    def __init__(self, request_callback: web.Application, decompress_request: bool, ssl_options: ssl.SSLContext) -> None: ...
    def stop(self) -> None: ...
    def listen(self, port: int) -> None: ...