import time
from operator import truediv
from threading import Thread

from .frame import Frame
import websocket
import logging
import time
import threading

VERSIONS = '1.0,1.1'

logger = logging.getLogger(__name__)

class Client:
    kill_now = False

    def __init__(self, url, headers={}):

        self.url = url
        self.ws = websocket.WebSocketApp(self.url, headers)
        self.ws.on_open = self._on_open
        self.ws.on_message = self._on_message
        self.ws.on_error = self._on_error
        self.ws.on_close = self._on_close
        self.ws.on_ping = self._on_wsping
        self.ws.on_pong = self._on_wspong

        self.opened = False

        self.connected = False

        self.counter = 0
        self.subscriptions = {}

        self._connectCallback = None
        self._errorCallback = None

        self.heartbeatsend = 5000
        self.heartbeatreceived = 5000
        self.heartbeatgrace = 3
        self.heartbeatgracefirst = 3
        self.hbfirst = True
        self.lastreceived = 0
        self.hbevent = None

    def _on_wsping(self, ws, data):
        logger.debug(str(threading.get_ident()) + " WS Ping received and already answered")

    def _on_wspong(self, ws, data):
        # Outgoing ping received
        logger.debug(str(threading.get_ident()) + " WS Pong recieved on our ping")

    def _connect(self, timeout=0):
        thread = Thread(target=self.ws.run_forever, kwargs={
            "ping_interval": 10,
            "ping_timeout": 3
            })
        thread.daemon = True
        thread.start()

        total_ms = 0
        while self.opened is False:
            time.sleep(.25)
            total_ms += 250
            if 0 < timeout < total_ms:
                raise TimeoutError(f"Connection to {self.url} timed out")

    def _on_open(self, ws_app, *args):
        self.opened = True

    def _on_close(self, ws_app, *args):
        self.connected = False
        logger.debug(str(threading.get_ident()) + " Whoops! Lost connection to " + self.ws.url)
        self._clean_up()

    def _on_error(self, ws_app, error, *args):
        logger.error(str(threading.get_ident()) + " error")
        logger.error(error)
        if self._errorCallback is not None:
            logger.error(str(threading.get_ident()) + " error callback")
            self._errorCallback(error)
        else:
            logger.error(str(threading.get_ident()) + " error callback none")

    def sendHeartbeat(self):
        while not self.kill_now:
            logger.info(str(threading.get_ident()) + " send haeartbeat")
            self.ws.send("\n")
            time.sleep(self.hbsendinterval / 1000)
        logger.info(str(threading.get_ident()) + " send haeartbeat terminated")

    def scheduleHeartbeatReceived(self):
        logger.info(str(threading.get_ident()) + " scheduleHeartbeatReceived")
        thread = Thread(target=self.checkHeartbeatReceived)
        thread.daemon = True
        thread.start()

    def checkHeartbeatReceived(self):
        logger.debug(str(threading.get_ident()) + " checkHeartbeatReceived")
        while self.connected:
            e = threading.Event()
            self.hbevent = e

            waittime = (self.receiveinterval * self.heartbeatgrace) / 1000
            if self.hbfirst:
                waittime = (self.receiveinterval * self.heartbeatgracefirst) / 1000
                self.hbfirst = False
            logger.info("waittime" + str(waittime))
            e.wait(waittime)
            if (time.time() > waittime + self.lastreceived):
                logger.info(str(threading.get_ident()) + " HB timeout")
                break
            else:
                logger.info(str(threading.get_ident()) + " HB ok")

        if self.connected:
            self.scheduleHeartbeatReceived()

    def initHeartbeat(self, frame):
        logger.debug(str(threading.get_ident()) + " initHeartbeat")
        hb = frame.headers['heart-beat']
        sx, sy = hb.split(",")

        self.hbsendinterval = max(int(sx), self.heartbeatsend)
        logger.info("set hbsendinterval to " + str(self.hbsendinterval))

        thread = Thread(target=self.sendHeartbeat)
        thread.daemon = True
        thread.start()

        self.receiveinterval = max(int(sy), self.heartbeatreceived)
        self.lastreceived = time.time()
        self.scheduleHeartbeatReceived()



    def _on_message(self, ws_app, message, *args):
        logger.info(str(threading.get_ident()) + "OnMessage")
        logger.info(message)
        logger.debug(str(threading.get_ident()) + "Message Content\n<<< " + str(message))
        _results = []

        self.lastreceived = time.time()
        if (self.hbevent is not None):
            self.hbevent.set()

        if str(message) == "\n" :
            logger.info(str(threading.get_ident()) + "stomp haertbeat recieved")
            if self.pingCallback is not None:
                self.pingCallback()
            return
        else:
            frame = Frame.unmarshall_single(message)

            if frame.command == "CONNECTED":
                self.connected = True
                logger.debug(str(threading.get_ident()) + "connected to server " + self.url)
                self.initHeartbeat(frame)

                if self._connectCallback is not None:
                    _results.append(self._connectCallback(frame))
            elif frame.command == "MESSAGE":
                logger.info(str(threading.get_ident()) + "message recieved")
                subscription = frame.headers['subscription']

                if subscription in self.subscriptions:
                    onreceive = self.subscriptions[subscription]
                    messageID = frame.headers['message-id']

                    def ack(headers):
                        if headers is None:
                            headers = {}
                        return self.ack(messageID, subscription, headers)

                    def nack(headers):
                        if headers is None:
                            headers = {}
                        return self.nack(messageID, subscription, headers)

                    frame.ack = ack
                    frame.nack = nack

                    _results.append(onreceive(frame))
                else:
                    info = "Unhandled received MESSAGE: " + str(frame)
                    logger.debug(info)
                    _results.append(info)
            elif frame.command == 'RECEIPT':
                pass
            elif frame.command == 'ERROR':
                logger.debug(str(threading.get_ident()) + "error recieved")
                if self._errorCallback is not None:
                    logger.debug(str(threading.get_ident()) + "error callback")
                    _results.append(self._errorCallback(frame))
            else:
                info = "Unhandled received MESSAGE: " + frame.command
                logger.debug(str(threading.get_ident()) + " " + info)
                _results.append(info)

        return _results

    def _transmit(self, command, headers, body=None):
        out = Frame.marshall(command, headers, body)
        logger.debug("\n>>> " + out)
        self.ws.send(out)

    def connect(self, login=None, passcode=None, headers=None, connectCallback=None, errorCallback=None,
                timeout=0, pingCallback=None):
        self._connectCallback = connectCallback
        self._errorCallback = errorCallback
        self.pingCallback = pingCallback

        logger.debug(str(threading.get_ident()) + "Opening web socket...")
        self._connect(timeout)

        headers = headers if headers is not None else {}
        headers['host'] = self.url
        headers['accept-version'] = VERSIONS
        headers['heart-beat'] = str(self.heartbeatsend) + ',' + str(self.heartbeatreceived)

        if login is not None:
            headers['login'] = login
        if passcode is not None:
            headers['passcode'] = passcode

        self._transmit('CONNECT', headers)

    def disconnect(self, disconnectCallback=None, headers=None):
        logger.debug(str(threading.get_ident()) + " >disconnect entering")
        self.kill_now = True
        if headers is None:
            headers = {}

        self._transmit("DISCONNECT", headers)
        self.ws.on_close = None
        self.ws.close()
        self._clean_up()

        if disconnectCallback is not None:
            logger.debug(str(threading.get_ident()) + " disconnect callback")
            disconnectCallback()
        logger.debug(str(threading.get_ident()) + " <disconnect exiting")

    def _clean_up(self):
        logger.debug(str(threading.get_ident()) + " _clean_up")
        self.connected = False

    def send(self, destination, headers=None, body=None):
        if headers is None:
            headers = {}
        if body is None:
            body = ''
        headers['destination'] = destination
        return self._transmit("SEND", headers, body)

    def subscribe(self, destination, callback=None, headers=None):
        if headers is None:
            headers = {}
        if 'id' not in headers:
            headers["id"] = "sub-" + str(self.counter)
            self.counter += 1
        headers['destination'] = destination
        self.subscriptions[headers["id"]] = callback
        self._transmit("SUBSCRIBE", headers)

        def unsubscribe():
            self.unsubscribe(headers["id"])

        return headers["id"], unsubscribe

    def unsubscribe(self, id):
        del self.subscriptions[id]
        return self._transmit("UNSUBSCRIBE", {
            "id": id
        })

    def ack(self, message_id, subscription, headers):
        if headers is None:
            headers = {}
        headers["message-id"] = message_id
        headers['subscription'] = subscription
        return self._transmit("ACK", headers)

    def nack(self, message_id, subscription, headers):
        if headers is None:
            headers = {}
        headers["message-id"] = message_id
        headers['subscription'] = subscription
        return self._transmit("NACK", headers)
