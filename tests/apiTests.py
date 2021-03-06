#
# Copyright 2012 Adam Litke, IBM Corporation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import logging
import socket
import json
import struct
from contextlib import closing

from testrunner import VdsmTestCase as TestCaseBase
import BindingJsonRpc
import yajsonrpc
import apiData
from jsonRpcUtils import getFreePort


ip = '127.0.0.1'
port = 9824
id = '2c8134fd-7dd4-4cfc-b7f8-6b7549399cb6'
_fakeret = {}

apiWhitelist = ('StorageDomain.Classes', 'StorageDomain.Types',
                'Volume.Formats', 'Volume.Types', 'Volume.Roles',
                'Image.DiskTypes', 'ConnectionRefs.ctorArgs',
                'Global.ctorArgs', 'ISCSIConnection.ctorArgs',
                'Image.ctorArgs', 'LVMVolumeGroup.ctorArgs',
                'StorageDomain.ctorArgs', 'StoragePool.ctorArgs',
                'Task.ctorArgs', 'VM.ctorArgs', 'Volume.ctorArgs')


def createFakeAPI():
    """
    Create a Mock API module for testing.  Mock API will return data from
    the _fakeret global variable instead of calling into vdsm.  _fakeret is
    expected to have the following format:

    {
      '<class1>': {
        '<func1>': [ <ret1>, <ret2>, ... ],
        '<func2>': [ ... ],
      }, '<class2>': {
        ...
      }
    }
    """
    class FakeObj(object):
        def __new__(cls, *args, **kwargs):
            return object.__new__(cls)

        def default(self, *args, **kwargs):
            try:
                return _fakeret[self.type][self.lastFunc].pop(0)
            except (KeyError, IndexError):
                raise Exception("No API data avilable for %s.%s" %
                                (self.type, self.lastFunc))

        def __getattr__(self, name):
            # While we are constructing the API module, use the normal getattr
            if 'API' not in sys.modules:
                return object.__getattr__(name)
            self.lastFunc = name
            return self.default

    import sys
    import imp
    from new import classobj

    _API = __import__('API', globals(), locals(), {}, -1)
    _newAPI = imp.new_module('API')

    for obj in ('Global', 'ConnectionRefs', 'StorageDomain', 'Image', 'Volume',
                'Task', 'StoragePool', 'VM'):
        cls = classobj(obj, (FakeObj,), {'type': obj})
        setattr(_newAPI, obj, cls)

    # Apply the whitelist to our version of API
    for name in apiWhitelist:
        parts = name.split('.')
        dstObj = _newAPI
        srcObj = _API
        # Walk the object hierarchy copying each component of the whitelisted
        # attribute from the real API to our fake one
        for obj in parts:
            srcObj = getattr(srcObj, obj)
            try:
                dstObj = getattr(dstObj, obj)
            except AttributeError:
                setattr(dstObj, obj, srcObj)

    # Install our fake API into the module table for use by the whole program
    sys.modules['API'] = _newAPI


def setUpModule():
    """
    Set up the environment for all tests:
    1. Override the API so we can program our own return values
    2. Start an embedded server to process our requests
    """
    global port
    log = logging.getLogger('apiTests')
    handler = logging.StreamHandler()
    fmt_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(fmt_str)
    handler.setFormatter(formatter)
    handler.setLevel(logging.INFO)
    log.addHandler(handler)

    createFakeAPI()

    # Bridge imports the API module so we must set up the fake API first
    import Bridge
    bridge = Bridge.DynamicBridge()

    # Support parallel testing.  Try hard to find an open port to use
    port = getFreePort()
    server = BindingJsonRpc.BindingJsonRpc(bridge,
                                           [('tcp', {"ip": ip,
                                                     "port": port})])
    server.start()


class APITest(TestCaseBase):
    def expectAPI(self, obj, meth, retval):
        global _fakeret
        if obj not in _fakeret:
            _fakeret[obj] = {}
        if meth not in _fakeret[obj]:
            _fakeret[obj][meth] = []
        _fakeret[obj][meth].append(retval)

    def programAPI(self, key):
        key += '_apidata'
        for item in getattr(apiData, key):
            self.expectAPI(item.obj, item.meth, item.data)

    def clearAPI(self):
        global _fakeret
        _fakeret = {}


class ConnectionError(Exception):
    pass


class ProtocolError(Exception):
    pass


class JsonRawTest(APITest):
    _Size = struct.Struct("!Q")

    def buildMessage(self, data):
        msg = json.dumps(data)
        msg = msg.encode('utf-8')
        msize = JsonRawTest._Size.pack(len(msg))
        resp = msize + msg
        return resp

    def _createRequest(self, method, reqId=None, params=()):
        return {'jsonrpc': '2.0', "id": reqId, "method": method,
                "params": params}

    def sendMessage(self, msg):
        with closing(socket.socket(socket.AF_INET,
                                   socket.SOCK_STREAM)) as sock:
            sock.settimeout(3)
            try:
                sock.connect((ip, port))
            except socket.error as e:
                raise ConnectionError("Unable to connect to server: %s" % e)
            try:
                sock.sendall(msg)
            except (socket.error, socket.timeout), e:
                raise ProtocolError("Unable to send request: %s" % e)
            try:
                data = sock.recv(JsonRawTest._Size.size)
            except socket.error as e:
                raise ProtocolError("Unable to read response length: %s" % e)
            if not data:
                raise ProtocolError("No data received")
            msgLen = JsonRawTest._Size.unpack(data)[0]
            try:
                data = sock.recv(msgLen)
            except socket.error as e:
                raise ProtocolError("Unable to read response body: %s" % e)
            if len(data) != msgLen:
                raise ProtocolError("Response body length mismatch")
            return json.loads(data)

    def testPing(self):
        self.clearAPI()
        self.programAPI("testPing")
        msg = self.buildMessage({'jsonrpc': '2.0',
                                 'id': id,
                                 'method': 'Host.ping',
                                 'params': {}})
        reply = self.sendMessage(msg)
        self.assertNotIn('error', reply)
        self.assertTrue(reply['result'])

    def testPingError(self):
        self.clearAPI()
        self.programAPI("testPingError")
        msg = self.buildMessage({'jsonrpc': '2.0',
                                 'id': id,
                                 'method': 'Host.ping',
                                 'params': {}})
        reply = self.sendMessage(msg)
        self.assertEquals(1, reply['error']['code'])
        self.assertNotIn('result', reply)

    def testNoMethod(self):
        msg = self.buildMessage({'jsonrpc': '2.0',
                                 'id': id,
                                 'method': 'Host.fake'})
        reply = self.sendMessage(msg)
        self.assertEquals(yajsonrpc.JsonRpcMethodNotFoundError().code,
                          reply['error']['code'])

    def testBadMethod(self):
        msg = self.buildMessage(self._createRequest('malformed\'', id))
        reply = self.sendMessage(msg)
        self.assertEquals(yajsonrpc.JsonRpcMethodNotFoundError().code,
                          reply['error']['code'])

    def testMissingSize(self):
        self.assertRaises(ProtocolError, self.sendMessage,
                          "malformed message")

    def testClientNotJson(self):
        msg = "malformed message"
        msize = JsonRawTest._Size.pack(len(msg))
        msg = msize + msg
        reply = self.sendMessage(msg)
        self.assertEquals(yajsonrpc.JsonRpcParseError().code,
                          reply['error']['code'])

    def testSynchronization(self):
        def doPing(msg):
            self.clearAPI()
            self.programAPI("testPing")
            ret = self.sendMessage(msg)
            self.assertNotIn('error', ret)

        msg = self.buildMessage({'jsonrpc': '2.0', 'id': id,
                                 'method': 'Host.ping'})
        # Send Truncated message
        self.assertRaises(ProtocolError, doPing, msg[:-1])

        # Test that the server recovers
        doPing(msg)

        # Send too much data
        doPing(msg + "Hello")

        # Test that the server recovers
        doPing(msg)

    def testInternalError(self):
        msg = self.buildMessage({'jsonrpc': '2.0',
                                 'id': id,
                                 'method': 'Host.ping'})
        reply = self.sendMessage(msg)
        self.assertEquals(yajsonrpc.JsonRpcInternalError().code,
                          reply['error']['code'])
