import json
import logging
from base64 import (
    b64decode,
    b64encode,
    binascii,
)
from collections import namedtuple
from time import time as now

import pycurl
from tornado.curl_httpclient import CurlError
from tornado.gen import convert_yielded
from tornado.httpclient import (
    AsyncHTTPClient,
    HTTPClientError,
)
from tornado.httputil import (
    HTTPHeaders,
    HTTPServerRequest,
)
from tornado.web import HTTPError

from pcs.common.tools import StringCollection
from pcs.daemon import log

SINATRA_GUI = "sinatra_gui"
SINATRA_REMOTE = "sinatra_remote"
SYNC_CONFIGS = "sync_configs"

DEFAULT_SYNC_CONFIG_DELAY = 5
RUBY_LOG_LEVEL_MAP = {
    "UNKNOWN": logging.NOTSET,
    "FATAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

__id_dict = {"id": 0}


def get_request_id():
    __id_dict["id"] += 1
    return __id_dict["id"]


class SinatraResult(namedtuple("SinatraResult", "headers, status, body")):
    @classmethod
    def from_response(cls, response):
        return cls(response["headers"], response["status"], response["body"])


def log_group_id_generator():
    group_id = 0
    while True:
        group_id = group_id + 1 if group_id < 99999 else 0
        yield group_id


LOG_GROUP_ID = log_group_id_generator()


def process_response_logs(rb_log_list):
    if not rb_log_list:
        return

    group_id = next(LOG_GROUP_ID)
    for rb_log in rb_log_list:
        log.from_external_source(
            level=RUBY_LOG_LEVEL_MAP.get(rb_log["level"], logging.NOTSET),
            created=rb_log["timestamp_usec"] / 1000000,
            usecs=int(str(rb_log["timestamp_usec"])[-6:]),
            message=rb_log["message"],
            group_id=group_id,
        )


class RubyDaemonRequest(
    namedtuple(
        "RubyDaemonRequest", "request_type, path, query, headers, method, body"
    )
):
    def __new__(
        cls,
        request_type,
        http_request: HTTPServerRequest = None,
        payload=None,
    ):
        headers = http_request.headers if http_request else HTTPHeaders()
        headers.add("X-Pcsd-Type", request_type)
        if payload:
            headers.add(
                "X-Pcsd-Payload",
                b64encode(json.dumps(payload).encode()).decode(),
            )
        return super(RubyDaemonRequest, cls).__new__(
            cls,
            request_type,
            http_request.path if http_request else "",
            http_request.query if http_request else "",
            headers,
            http_request.method if http_request else "GET",
            http_request.body if http_request else None,
        )

    @property
    def url(self):
        # We do not need location for communication with ruby itself since we
        # communicate via unix socket. But it is required by AsyncHTTPClient so
        # "localhost" is used.
        query = f"?{self.query}" if self.query else ""
        return f"localhost/{self.path}{query}"

    @property
    def is_get(self):
        return self.method.upper() == "GET"

    @property
    def has_http_request_detail(self):
        return self.path or self.query or self.method != "GET" or self.body


def log_ruby_daemon_request(label, request: RubyDaemonRequest):
    log.pcsd.debug("%s type: '%s'", label, request.request_type)
    if request.has_http_request_detail:
        log.pcsd.debug("%s path: '%s'", label, request.path)
        if request.query:
            log.pcsd.debug("%s query: '%s'", label, request.query)
        log.pcsd.debug("%s method: '%s'", label, request.method)
        if request.body:
            log.pcsd.debug("%s body: '%s'", label, request.body)


class Wrapper:
    def __init__(self, pcsd_ruby_socket, debug=False):
        self.__debug = debug
        AsyncHTTPClient.configure("tornado.curl_httpclient.CurlAsyncHTTPClient")
        self.__client = AsyncHTTPClient()
        self.__pcsd_ruby_socket = pcsd_ruby_socket

    def prepare_curl_callback(self, curl):
        curl.setopt(pycurl.UNIX_SOCKET_PATH, self.__pcsd_ruby_socket)
        curl.setopt(pycurl.TIMEOUT, 0)

    async def send_to_ruby(self, request: RubyDaemonRequest):
        try:
            return (
                await self.__client.fetch(
                    request.url,
                    headers=request.headers,
                    method=request.method,
                    # Tornado enforces body=None for GET method:
                    # Even with `allow_nonstandard_methods` we disallow GET
                    # with a body (because libcurl doesn't allow it unless we
                    # use CUSTOMREQUEST).  While the spec doesn't forbid
                    # clients from sending a body, it arguably disallows the
                    # server from doing anything with them.
                    body=(request.body if not request.is_get else None),
                    prepare_curl_callback=self.prepare_curl_callback,
                )
            ).body
        except CurlError as e:
            # This error we can get e.g. when ruby daemon is down.
            log.pcsd.error(
                "Cannot connect to ruby daemon (message: '%s'). Is it running?",
                e,
            )
            raise HTTPError(500) from e
        except HTTPClientError as e:
            # This error we can get e.g. when rack protection raises exception.
            log.pcsd.error(
                (
                    "Got error from ruby daemon (message: '%s')."
                    " Try checking system logs (e.g. journal, systemctl status"
                    " pcsd.service) for more information.."
                ),
                e,
            )
            raise HTTPError(500) from e

    async def run_ruby(
        self,
        request_type,
        http_request: HTTPServerRequest = None,
        payload=None,
    ):
        request = RubyDaemonRequest(request_type, http_request, payload)
        request_id = get_request_id()

        def log_request():
            log_ruby_daemon_request(
                f"Ruby daemon request (id: {request_id})",
                request,
            )

        if self.__debug:
            log_request()

        return self.process_ruby_response(
            f"Ruby daemon response (id: {request_id})",
            log_request,
            await self.send_to_ruby(request),
        )

    def process_ruby_response(self, label, log_request, ruby_response):
        """
        Return relevant part of unpacked ruby response. As a side effect
        relevant logs are written.

        string label -- is used as a log prefix
        callable log_request -- is used to log request when some errors happen;
            we want to log request before error even if there is not debug mode
        string ruby_response -- body of response from ruby; it should contain
            json with dictionary with response specific keys
        """
        try:
            response = json.loads(ruby_response)
            if "error" in response:
                if not self.__debug:
                    log_request()
                log.pcsd.error(
                    "%s contains an error: '%s'", label, json.dumps(response)
                )
                raise HTTPError(500)

            logs = response.pop("logs", [])
            if "body" in response:
                body = b64decode(response.pop("body"))
                if self.__debug:
                    log.pcsd.debug(
                        "%s (without logs and body): '%s'",
                        label,
                        json.dumps(response),
                    )
                    log.pcsd.debug("%s body: '%s'", label, body)
                response["body"] = body

            elif self.__debug:
                log.pcsd.debug(
                    "%s (without logs): '%s'", label, json.dumps(response)
                )
            process_response_logs(logs)
            return response
        except (json.JSONDecodeError, binascii.Error) as e:
            if self.__debug:
                log.pcsd.debug("%s: '%s'", label, ruby_response)
            else:
                log_request()

            log.pcsd.error("Cannot decode json from ruby pcsd wrapper: '%s'", e)
            raise HTTPError(500) from e

    async def request_gui(
        self, request: HTTPServerRequest, user: str, groups: StringCollection
    ) -> SinatraResult:
        # Sessions handling was removed from ruby. However, some session
        # information is needed for ruby code (e.g. rendering some parts of
        # templates). So this information must be sent to ruby by another way.
        return SinatraResult.from_response(
            await convert_yielded(
                self.run_ruby(
                    SINATRA_GUI,
                    request,
                    {
                        "username": user,
                        "groups": list(groups),
                    },
                )
            )
        )

    async def request_remote(self, request: HTTPServerRequest) -> SinatraResult:
        return SinatraResult.from_response(
            await convert_yielded(self.run_ruby(SINATRA_REMOTE, request))
        )

    async def sync_configs(self):
        try:
            return (await convert_yielded(self.run_ruby(SYNC_CONFIGS)))["next"]
        except HTTPError:
            log.pcsd.error("Config synchronization failed")
            return int(now()) + DEFAULT_SYNC_CONFIG_DELAY
