#!/usr/bin/env python3
################################
# Interactive UPNP application #
# Craig Heffner                #
# 07/16/2008                   #
################################
# Python 3.12.3 Adaptation     #
################################                                                                            #

import sys
import os
import re
import platform
import xml.dom.minidom as minidom
import urllib
import urllib.request
import readline
import time
import pickle
import struct
import base64
import getopt
import select
import fcntl
import subprocess
from socket import *

def interface_exists(iface):
    """ Check if interface exists """
    try:
        if platform.system() == 'Windows':
            result = subprocess.run(['netsh', 'interface', 'show', 'interface'],
                                    capture_output=True,
                                    text=True).stdout
        else:
            result = os.listdir('/sys/class/net/')
        return iface in result
    except Exception as e:
        print(e)
        return False

def get_ip_address(ifname):
    s = socket(AF_INET, SOCK_DGRAM)
    ifname = ifname.encode('utf-8')
    return inet_ntoa(fcntl.ioctl(
        s.fileno(),
        0x8915,  # SIOCGIFADDR
        struct.pack('256s', ifname[:15])
    )[20:24])
# Most of the CmdCompleter class was originally written by John Kenyan
# It serves to tab-complete commands inside the program's shell
class CmdCompleter:
    """
    Most of the CmdCompleter class was originally written by John Kenyan
    It serves to tab-complete commands inside the program's shell
    """

    def __init__(self, commands):
        self.commands = commands

    def traverse(self, tokens, tree):
        """ Traverses the list of available commands """
        retVal = []
        # If there are no commands, or no user input, return null
        if tree is None or len(tokens) == 0:
            retVal = []
        # If there is only one word, only auto-complete the primary commands
        elif len(tokens) == 1:
            retVal = [x + ' ' for x in tree if x.startswith(tokens[0])]
        # Else auto-complete for the sub-commands
        elif tokens[0] in tree.keys():
            retVal = self.traverse(tokens[1:], tree[tokens[0]])
        return retVal

    def complete(self, text, state):
        """ Returns a list of possible commands that match the partial command that the user has entered"""
        try:
            tokens = readline.get_line_buffer().split()
            if not tokens or readline.get_line_buffer()[-1] == ' ':
                tokens.append('')
            results = self.traverse(tokens, self.commands) + [None]
            return results[state]
        except Exception as e:
            print("Failed to complete command: %s" % str(e))

        return


class upnp:
    """ UPNP class for getting, sending and parsing SSDP/SOAP XML data (among other things...) """
    ip = False
    port = False
    completer = False
    msearchHeaders = {
        'MAN': '"ssdp:discover"',
        'MX': '2'
    }
    DEFAULT_IP = "239.255.255.250"
    DEFAULT_PORT = 1900
    UPNP_VERSION = '1.0'
    MAX_RECV = 8192
    MAX_HOSTS = 0
    TIMEOUT = 0
    HTTP_HEADERS = []
    ENUM_HOSTS = {}
    VERBOSE = False
    UNIQ = False
    DEBUG = False
    LOG_FILE = None
    BATCH_FILE = None
    IFACE = None
    STARS = '****************************************************************'
    csock: socket = None
    ssock: socket = None

    def __init__(self, ip, port, iface, appCommands):
        self.mreq = None
        if appCommands:
            self.completer = CmdCompleter(appCommands)
        if not self.initSockets(ip, port, iface):
            print('UPNP class initialization failed!')
            print('Bye!')
            sys.exit(1)
        else:
            self.soapEnd = re.compile('</.*:envelope>')

    def initSockets(self, ip, port, iface):
        """ Initialize default sockets"""
        if self.csock:
            self.csock.close()
        if self.ssock:
            self.ssock.close()

        if iface:
            self.IFACE = iface
        if not ip:
            ip = self.DEFAULT_IP
        if not port:
            port = self.DEFAULT_PORT
        self.port = port
        self.ip = ip

        try:
            # This is needed to join a multicast group
            self.mreq = struct.pack("4sl", inet_aton(ip), INADDR_ANY)

            # Set up client socket
            self.csock = socket(AF_INET, SOCK_DGRAM)
            self.csock.setsockopt(IPPROTO_IP, IP_MULTICAST_TTL, 2)

            # Set up server socket
            self.ssock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
            self.ssock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
            self.ssock.setsockopt(IPPROTO_IP, IP_MULTICAST_TTL, 32)
            self.ssock.setsockopt(IPPROTO_IP, IP_MULTICAST_LOOP, 1)

            # BSD systems also need to set SO_REUSEPORT
            try:
                self.ssock.setsockopt(SOL_SOCKET, SO_REUSEPORT, 1)
            except Exception as e:
                print("WARNING: sock.setsockopt: ", e)

            try:
                self.ssock.bind((self.ip, self.port))
                host = gethostbyname(gethostname())
                if self.IFACE:
                    iface_ip = get_ip_address(self.IFACE)
                    print("\nBinding to %s interface IP: %s" % (self.IFACE, iface_ip))
                    self.ssock.setsockopt(SOL_IP, IP_MULTICAST_IF, inet_aton(iface_ip))
                    self.ssock.setsockopt(SOL_IP, IP_ADD_MEMBERSHIP, inet_aton(self.ip) + inet_aton(iface_ip))
            except Exception as e:
                print("WARNING: Failed to bind %s:%d: %s" % (self.ip, self.port, e))
                raise e
            try:
                self.ssock.setsockopt(IPPROTO_IP, IP_ADD_MEMBERSHIP, self.mreq)
            except Exception as e:
                print('WARNING: Failed to join multicast group:', e)
        except Exception as e:
            print("Failed to initialize UPNP sockets:", e)
            return False
        return True

    def cleanup(self):
        """ Clean up file/socket descriptors """
        if self.LOG_FILE:
            self.LOG_FILE.close()
        self.csock.close()
        self.ssock.close()

    def send(self, data, sock):
        """ Send network data """
        # By default, use the client socket that's part of this class
        if not sock:
            sock = self.csock
        try:
            data = data.encode('utf-8')
            sock.sendto(data, (self.ip, self.port))
            return True
        except Exception as e:
            print("SendTo method failed for %s:%d : %s" % (self.ip, self.port, e))
            return False

    def recv(self, size, _socket):
        """ Receive network data """
        if not _socket:
            _socket = self.ssock

        if self.TIMEOUT:
            _socket.setblocking(False)
            ready = select.select([_socket], [], [], self.TIMEOUT)[0]
        else:
            _socket.setblocking(True)
            ready = True

        try:
            if ready:
                return _socket.recv(size)
            else:
                return False
        except:
            return False

    @staticmethod
    def createNewListener(ip, port):
        """ Create new UDP socket on ip, bound to port """
        try:
            newsock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
            newsock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
            # BSD systems also need to set SO_REUSEPORT
            try:
                newsock.setsockopt(SOL_SOCKET, SO_REUSEPORT, 1)
            except:
                pass
            newsock.bind((ip, port))
            return newsock
        except:
            return False

    def listener(self):
        """ Return the class's primary server socket """
        return self.ssock

    def sender(self):
        """ Return the class's primary client socket """
        return self.csock

    @staticmethod
    def parseURL(url):
        """ Parse a URL, return the host and the page """
        delim = '://'
        host = None
        page = None

        # Split the host and page
        try:
            (host, page) = url.split(delim)[1].split('/', 1)
            page = '/' + page
        except:
            # If '://' is not in the url, then it's not a full URL, so assume that it's just a relative path
            page = url

        return host, page

    @staticmethod
    def parseDeviceTypeName(string):
        """
        Pull the name of the device type from a device type string
        The device type string looks like: 'urn:schemas-upnp-org:device:WANDevice:1'
        """
        delim1 = 'device:'
        delim2 = ':'

        if delim1 in string and not string.endswith(delim1):
            return string.split(delim1)[1].split(delim2, 1)[0]
        return False

    @staticmethod
    def parseServiceTypeName(string):
        """
        Pull the name of the service type from a service type string
        The service type string looks like: 'urn:schemas-upnp-org:service:Layer3Forwarding:1'
        """
        delim1 = 'service:'
        delim2 = ':'

        if delim1 in string and not string.endswith(delim1):
            return string.split(delim1)[1].split(delim2, 1)[0]
        return False

    @staticmethod
    def parseHeader(data, header):
        """ Pull the header info for the specified HTTP header - case-insensitive """
        delimiter = "%s:" % header
        defaultRet = False

        lowerDelim = delimiter.lower()
        dataArray = data.split("\r\n")

        # Loop through each line of the headers
        for line in dataArray:
            lowerLine = line.lower()
            # Does this line start with the header we're looking for?
            if lowerLine.startswith(lowerDelim):
                try:
                    return line.split(':', 1)[1].strip()
                except:
                    print("Failure parsing header data for %s" % header)
        return defaultRet

    @staticmethod
    def extractSingleTag(data, tag):
        """ Extract the contents of a single XML tag from the data """
        startTag = "<%s" % tag
        endTag = "</%s>" % tag

        try:
            tmp = data.split(startTag)[1]
            index = tmp.find('>')
            if index != -1:
                index += 1
                return tmp[index:].split(endTag)[0].strip()
        except:
            pass
        return None

    def parseSSDPInfo(self, data, showUniq, verbose):
        """ Parses SSDP notify and reply packets, and populates the ENUM_HOSTS dict """
        hostFound = False
        foundLocation = False
        messageType = False
        xmlFile = False
        host = False
        page = False
        upnpType = None
        knownHeaders = {
            'NOTIFY': 'notification',
            'HTTP/1.1 200 OK': 'reply'
        }

        # Use the class defaults if these aren't specified
        if not showUniq:
            showUniq = self.UNIQ
        if not verbose:
            verbose = self.VERBOSE

        # Is the SSDP packet a notification, a reply, or neither?
        data = data.decode('utf-8')
        for text, messageType in knownHeaders.items():
            if data.upper().startswith(text):
                break
            else:
                messageType = False

        # If this is a notification or a reply message...
        if messageType:
            # Get the host name and location of its main UPNP XML file
            xmlFile = self.parseHeader(data, "LOCATION")
            upnpType = self.parseHeader(data, "SERVER")
            (host, page) = self.parseURL(xmlFile)

            # Sanity check to make sure we got all the info we need
            if xmlFile == False or host == False or page == False:
                print('ERROR parsing recieved header:')
                print(self.STARS)
                print(data)
                print(self.STARS)
                print('')
                return False

            # Get the protocol in use (i.e., http, https, etc)
            protocol = xmlFile.split('://')[0] + '://'

            # Check if we've seen this host before; add to the list of hosts if:
            #    1. This is a new host
            #    2. We've already seen this host, but the uniq hosts setting is disabled
            for hostID, hostInfo in self.ENUM_HOSTS.items():
                if hostInfo['name'] == host:
                    hostFound = True
                    if self.UNIQ:
                        return False

            if (hostFound and not self.UNIQ) or not hostFound:
                # Get the new host's index number and create an entry in ENUM_HOSTS
                index = len(self.ENUM_HOSTS)
                self.ENUM_HOSTS[index] = {
                    'name': host,
                    'dataComplete': False,
                    'proto': protocol,
                    'xmlFile': xmlFile,
                    'serverType': None,
                    'upnpServer': upnpType,
                    'deviceList': {}
                }
                # Be sure to update the command completer so we can tab complete through this host's data structure
                self.updateCmdCompleter(self.ENUM_HOSTS)

            # print(out some basic device info)
            print(self.STARS)
            print("SSDP %s message from %s" % (messageType, host))

            if xmlFile:
                foundLocation = True
                print("XML file is located at %s" % xmlFile)

            if upnpType:
                print("Device is running %s" % upnpType)

            print(self.STARS)
            print('')

            return True

    def getXML(self, url):
        """ Send GET request for a UPNP XML file """
        headers = {
            'USER-AGENT': 'uPNP/' + self.UPNP_VERSION,
            'CONTENT-TYPE': 'text/xml; charset="utf-8"'
        }

        try:
            # python3 - urllib is urllib3
            req = urllib.request.Request(url, None, headers)
            response = urllib.request.urlopen(req)
            output = response.read()
            headers = response.info()
            return headers, output
        except Exception as e:
            print("Request for '%s' failed: %s" % (url, e))
            return False, False

    def sendSOAP(self, hostName, serviceType, controlURL, actionName, actionArguments):
        """ Send SOAP request """
        argList = ''
        soapResponse = ''

        if '://' in controlURL:
            urlArray = controlURL.split('/', 3)
            if len(urlArray) < 4:
                controlURL = '/'
            else:
                controlURL = '/' + urlArray[3]

        soapRequest = 'POST %s HTTP/1.1\r\n' % controlURL

        # Check if a port number was specified in the host name; default is port 80
        if ':' in hostName:
            hostNameArray = hostName.split(':')
            host = hostNameArray[0]
            try:
                port = int(hostNameArray[1])
            except:
                print('Invalid port specified for host connection:', hostName[1])
                return False
        else:
            host = hostName
            port = 80

        # Create a string containing all the SOAP action's arguments and values
        for arg, (val, dt) in actionArguments.items():
            argList += '<%s>%s</%s>' % (arg, val, arg)

        # Create the SOAP request
        soapBody = '<?xml version="1.0"?>\n' \
                   '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">\n' \
                   '<SOAP-ENV:Body>\n' \
                   '\t<m:%s xmlns:m="%s">\n' \
                   '%s\n' \
                   '\t</m:%s>\n' \
                   '</SOAP-ENV:Body>\n' \
                   '</SOAP-ENV:Envelope>' % (actionName, serviceType, argList, actionName)

        # Specify the headers to send with the request
        headers = {
            'Host': hostName,
            'Content-Length': len(soapBody),
            'Content-Type': 'text/xml',
            'SOAPAction': '"%s#%s"' % (serviceType, actionName)
        }

        # Generate the final payload
        for head, value in headers.items():
            soapRequest += '%s: %s\r\n' % (head, value)
        soapRequest += '\r\n%s' % soapBody

        # Send data and go into recieve loop
        sock = None
        try:
            sock = socket(AF_INET, SOCK_STREAM)
            sock.connect((host, port))

            if self.DEBUG:
                print(self.STARS)
                print(soapRequest)
                print(self.STARS)
                print('')

            sock.send(soapRequest.encode('utf-8'))
            while True:
                data = sock.recv(self.MAX_RECV)
                if not data:
                    break
                else:
                    soapResponse += data.decode('utf-8')
                    if self.soapEnd.search(soapResponse.lower()):
                        break
            sock.close()

            (header, body) = soapResponse.split('\r\n\r\n', 1)
            if not header.upper().startswith('HTTP/1.') and ' 200 ' in header.split('\r\n')[0]:
                print('SOAP request failed with error code:', header.split('\r\n')[0].split(' ', 1)[1])
                errorMsg = self.extractSingleTag(body, 'errorDescription')
                if errorMsg:
                    print('SOAP error message:', errorMsg)
                return False
            else:
                return body
        except Exception as e:
            print('Caught socket exception:', e)
            if sock:
                sock.close()
            return False
        except KeyboardInterrupt:
            print("")
            if sock:
                sock.close()
            return False

    def showCompleteHostInfo(self, index, fp):
        """ Display all info for a given host """
        na = 'N/A'
        serviceKeys = ['controlURL', 'eventSubURL', 'serviceId', 'SCPDURL', 'fullName']
        if not fp:
            fp = sys.stdout

        if index < 0 or index >= len(self.ENUM_HOSTS):
            fp.write('Specified host does not exist...\n')
            return
        try:
            hostInfo = self.ENUM_HOSTS[index]
            if not hostInfo['dataComplete']:
                print("Cannot show all host info because I don't have it all yet."
                      " Try running 'host info %d' first...\n" % index)
            fp.write('Host name:     %s\n' % hostInfo['name'])
            fp.write('UPNP XML File:     %s\n\n' % hostInfo['xmlFile'])

            fp.write('\nDevice information:\n')
            for deviceName, deviceStruct in hostInfo['deviceList'].items():
                fp.write('\tDevice Name: %s\n' % deviceName)
                for serviceName, serviceStruct in deviceStruct['services'].items():
                    fp.write('\t\tService Name: %s\n' % serviceName)
                    for key in serviceKeys:
                        fp.write('\t\t\t%s: %s\n' % (key, serviceStruct[key]))
                    fp.write('\t\t\tServiceActions:\n')
                    for actionName, actionStruct in serviceStruct['actions'].items():
                        fp.write('\t\t\t\t%s\n' % actionName)
                        for argName, argStruct in actionStruct['arguments'].items():
                            fp.write('\t\t\t\t\t%s \n' % argName)
                            for key, val in argStruct.items():
                                if key == 'relatedStateVariable':
                                    fp.write('\t\t\t\t\t\t%s:\n' % val)
                                    for k, v in serviceStruct['serviceStateVariables'][val].items():
                                        fp.write('\t\t\t\t\t\t\t%s: %s\n' % (k, v))
                                else:
                                    fp.write('\t\t\t\t\t\t%s: %s\n' % (key, val))

        except Exception as e:
            print('Caught exception while showing host info:', e)

    def getHostInfo(self, xmlData, xmlHeaders, index):
        """ Wrapper function """
        if self.ENUM_HOSTS[index]['dataComplete']:
            return

        if 0 <= index < len(self.ENUM_HOSTS):
            try:
                xmlRoot = minidom.parseString(xmlData)
                self.parseDeviceInfo(xmlRoot, index)
                self.ENUM_HOSTS[index]['serverType'] = xmlHeaders.get('Server')
                self.ENUM_HOSTS[index]['dataComplete'] = True
                return True
            except Exception as e:
                print('Caught exception while getting host info:', e)
        return False

    def parseDeviceInfo(self, xmlRoot, index):
        """ Parse device info from the retrieved XML file """
        deviceEntryPointer = False
        devTag = "device"
        deviceType = "deviceType"
        deviceListEntries = "deviceList"
        deviceTags = ["friendlyName", "modelDescription", "modelName", "modelNumber", "modelURL", "presentationURL",
                      "UDN", "UPC", "manufacturer", "manufacturerURL"]

        # Find all device entries listed in the XML file
        for device in xmlRoot.getElementsByTagName(devTag):
            try:
                # Get the deviceType string
                deviceTypeName = str(device.getElementsByTagName(deviceType)[0].childNodes[0].data)
            except:
                continue

            # Pull out the action device name from the deviceType string
            deviceDisplayName = self.parseDeviceTypeName(deviceTypeName)
            if not deviceDisplayName:
                continue

            # Create a new device entry for this host in the ENUM_HOSTS structure
            deviceEntryPointer = self.ENUM_HOSTS[index][deviceListEntries][deviceDisplayName] = {}
            deviceEntryPointer['fullName'] = deviceTypeName

            # Parse out all the device tags for that device
            for tag in deviceTags:
                try:
                    deviceEntryPointer[tag] = str(device.getElementsByTagName(tag)[0].childNodes[0].data)
                except Exception as e:
                    if self.VERBOSE:
                        print('Device', deviceEntryPointer['fullName'], 'does not have a', tag)
                    continue
            # Get a list of all services for this device listing
            self.parseServiceList(device, deviceEntryPointer, index)

        return

    def parseServiceList(self, xmlRoot, device, index):
        """ Parse the list of services specified in the XML file """
        serviceEntryPointer = False
        dictName = "services"
        serviceListTag = "serviceList"
        serviceTag = "service"
        serviceNameTag = "serviceType"
        serviceTags = ["serviceId", "controlURL", "eventSubURL", "SCPDURL"]

        try:
            device[dictName] = {}
            # Get a list of all services offered by this device
            for service in xmlRoot.getElementsByTagName(serviceListTag)[0].getElementsByTagName(serviceTag):
                # Get the full service descriptor
                serviceName = str(service.getElementsByTagName(serviceNameTag)[0].childNodes[0].data)

                # Get the service name from the service descriptor string
                serviceDisplayName = self.parseServiceTypeName(serviceName)
                if not serviceDisplayName:
                    continue

                # Create new service entry for the device in ENUM_HOSTS
                serviceEntryPointer = device[dictName][serviceDisplayName] = {}
                serviceEntryPointer['fullName'] = serviceName

                # Get all the required service info and add it to ENUM_HOSTS
                for tag in serviceTags:
                    serviceEntryPointer[tag] = str(service.getElementsByTagName(tag)[0].childNodes[0].data)

                # Get specific service info about this service
                self.parseServiceInfo(serviceEntryPointer, index)
        except Exception as e:
            print('Caught exception while parsing device service list:', e)

    def parseServiceInfo(self, service, index):
        """ Parse details about each service (arguements, variables, etc) """
        argIndex = 0
        argTags = ['direction', 'relatedStateVariable']
        actionList = 'actionList'
        actionTag = 'action'
        nameTag = 'name'
        argumentList = 'argumentList'
        argumentTag = 'argument'

        # Get the full path to the service's XML file
        xmlFile = self.ENUM_HOSTS[index]['proto'] + self.ENUM_HOSTS[index]['name']
        if not xmlFile.endswith('/') and not service['SCPDURL'].startswith('/'):
            try:
                xmlServiceFile = self.ENUM_HOSTS[index]['xmlFile']
                slashIndex = xmlServiceFile.rfind('/')
                xmlFile = xmlServiceFile[:slashIndex] + '/'
            except:
                xmlFile += '/'

        if self.ENUM_HOSTS[index]['proto'] in service['SCPDURL']:
            xmlFile = service['SCPDURL']
        else:
            xmlFile += service['SCPDURL']
        service['actions'] = {}

        # Get the XML file that describes this service
        (xmlHeaders, xmlData) = self.getXML(xmlFile)
        if not xmlData:
            print('Failed to retrieve service descriptor located at:', xmlFile)
            return False

        try:
            xmlRoot = minidom.parseString(xmlData)

            # Get a list of actions for this service
            try:
                actionList = xmlRoot.getElementsByTagName(actionList)[0]
            except:
                print('Failed to retrieve action list for service %s!' % service['fullName'])
                return False
            actions = actionList.getElementsByTagName(actionTag)
            if not actions:
                return False

            # Parse all actions in the service's action list
            for action in actions:
                # Get the action's name
                try:
                    actionName = str(action.getElementsByTagName(nameTag)[0].childNodes[0].data).strip()
                except:
                    print('Failed to obtain service action name (%s)!' % service['fullName'])
                    continue

                # Add the action to the ENUM_HOSTS dictonary
                service['actions'][actionName] = {}
                service['actions'][actionName]['arguments'] = {}

                # Parse all the action's arguments
                try:
                    argList = action.getElementsByTagName(argumentList)[0]
                except:
                    # Some actions may take no arguments, so continue without raising an error here...
                    continue

                # Get all the arguments in this action's argument list
                arguments = argList.getElementsByTagName(argumentTag)
                if not arguments:
                    if self.VERBOSE:
                        print('Action', actionName, 'has no arguments!')
                    continue

                # Loop through the action's arguments, appending them to the ENUM_HOSTS dictionary
                for argument in arguments:
                    try:
                        argName = str(argument.getElementsByTagName(nameTag)[0].childNodes[0].data)
                    except:
                        print('Failed to get argument name for', actionName)
                        continue
                    service['actions'][actionName]['arguments'][argName] = {}

                    # Get each required argument tag value and add them to ENUM_HOSTS
                    for tag in argTags:
                        try:
                            service['actions'][actionName]['arguments'][argName][tag] = str(
                                argument.getElementsByTagName(tag)[0].childNodes[0].data)
                        except:
                            print('Failed to find tag %s for argument %s!' % (tag, argName))
                            continue

            # Parse all the state variables for this service
            self.parseServiceStateVars(xmlRoot, service)

        except Exception as e:
            print('Caught exception while parsing Service info for service %s: %s' % (service['fullName'], str(e)))
            return False

        return True

    @staticmethod
    def parseServiceStateVars(xmlRoot, servicePointer):
        """ Get info about a service's state variables """
        na = 'N/A'
        varVals = ['sendEvents', 'dataType', 'defaultValue', 'allowedValues']
        serviceStateTable = 'serviceStateTable'
        stateVariable = 'stateVariable'
        nameTag = 'name'
        dataType = 'dataType'
        sendEvents = 'sendEvents'
        allowedValueList = 'allowedValueList'
        allowedValue = 'allowedValue'
        allowedValueRange = 'allowedValueRange'
        minimum = 'minimum'
        maximum = 'maximum'

        # Create the serviceStateVariables entry for this service in ENUM_HOSTS
        servicePointer['serviceStateVariables'] = {}

        # Get a list of all state variables associated with this service
        try:
            stateVars = xmlRoot.getElementsByTagName(serviceStateTable)[0].getElementsByTagName(stateVariable)
        except:
            # Don't necessarily want to throw an error here, as there may be no service state variables
            return False

        # Loop through all state variables
        for var in stateVars:
            for tag in varVals:
                # Get variable name
                try:
                    varName = str(var.getElementsByTagName(nameTag)[0].childNodes[0].data)
                except:
                    print('Failed to get service state variable name for service %s!' % servicePointer['fullName'])
                    continue

                servicePointer['serviceStateVariables'][varName] = {}
                try:
                    servicePointer['serviceStateVariables'][varName]['dataType'] = str(
                        var.getElementsByTagName(dataType)[0].childNodes[0].data)
                except:
                    servicePointer['serviceStateVariables'][varName]['dataType'] = na
                try:
                    servicePointer['serviceStateVariables'][varName]['sendEvents'] = str(
                        var.getElementsByTagName(sendEvents)[0].childNodes[0].data)
                except:
                    servicePointer['serviceStateVariables'][varName]['sendEvents'] = na

                servicePointer['serviceStateVariables'][varName][allowedValueList] = []

                # Get a list of allowed values for this variable
                try:
                    vals = var.getElementsByTagName(allowedValueList)[0].getElementsByTagName(allowedValue)
                except:
                    pass
                else:
                    # Add the list of allowed values to the ENUM_HOSTS dictionary
                    for val in vals:
                        servicePointer['serviceStateVariables'][varName][allowedValueList].append(
                            str(val.childNodes[0].data))

                # Get allowed value range for this variable
                try:
                    valList = var.getElementsByTagName(allowedValueRange)[0]
                except:
                    pass
                else:
                    # Add the max and min values to the ENUM_HOSTS dictionary
                    servicePointer['serviceStateVariables'][varName][allowedValueRange] = []
                    try:
                        servicePointer['serviceStateVariables'][varName][allowedValueRange].append(
                            str(valList.getElementsByTagName(minimum)[0].childNodes[0].data))
                        servicePointer['serviceStateVariables'][varName][allowedValueRange].append(
                            str(valList.getElementsByTagName(maximum)[0].childNodes[0].data))
                    except:
                        pass
        return True

    def updateCmdCompleter(self, struct):
        """ Update the command completer """
        indexOnlyList = {
            'host': ['get', 'details', 'summary'],
            'save': ['info']
        }
        hostCommand = 'host'
        subCommandList = ['info']
        sendCommand = 'send'

        try:
            structPtr = {}
            topLevelKeys = {}
            for key, val in struct.items():
                structPtr[str(key)] = val
                topLevelKeys[str(key)] = None

            # Update the subCommandList
            for subcmd in subCommandList:
                self.completer.commands[hostCommand][subcmd] = None
                self.completer.commands[hostCommand][subcmd] = structPtr

            # Update the indexOnlyList
            for cmd, data in indexOnlyList.items():
                for subcmd in data:
                    self.completer.commands[cmd][subcmd] = topLevelKeys

            # This is for updating the sendCommand key
            structPtr = {}
            for hostIndex, hostData in struct.items():
                host = str(hostIndex)
                structPtr[host] = {}
                if 'deviceList' in hostData.keys():
                    for device, deviceData in hostData['deviceList'].items():
                        structPtr[host][device] = {}
                        if 'services' in deviceData.keys():
                            for service, serviceData in deviceData['services'].items():
                                structPtr[host][device][service] = {}
                                if 'actions' in serviceData.keys():
                                    for action, actionData in serviceData['actions'].items():
                                        structPtr[host][device][service][action] = None
            self.completer.commands[hostCommand][sendCommand] = structPtr
        except Exception:
            print("Error updating command completer structure; some command completion features might not work...")
        return


################## Action Functions ######################
# These functions handle user commands from the shell    #
##########################################################

def msearch(argc, argv, hp):
    """ Actively search for UPNP devices """
    defaultST = "upnp:rootdevice"
    st = "schemas-upnp-org"
    myip = ''
    lport = hp.port

    if argc >= 3:
        if argc == 4:
            st = argv[1]
            searchType = argv[2]
            searchName = argv[3]
        else:
            searchType = argv[1]
            searchName = argv[2]
        st = "urn:%s:%s:%s:%s" % (st, searchType, searchName, hp.UPNP_VERSION.split('.')[0])
    else:
        st = defaultST

    # Build the request
    request = "M-SEARCH * HTTP/1.1\r\n" \
              "HOST:%s:%d\r\n" \
              "ST:%s\r\n" % (hp.ip, hp.port, st)
    for header, value in hp.msearchHeaders.items():
        request += header + ':' + value + "\r\n"
    request += "\r\n"

    print("Entering discovery mode for '%s', Ctl+C to stop..." % st)
    print('')

    # Have to create a new socket since replies will be sent directly to our IP, not the multicast IP
    server = hp.createNewListener(myip, lport)
    if not server:
        print('Failed to bind port %d' % lport)
        return

    hp.send(request, server)
    count = 0
    start = time.time()

    while True:
        try:
            if 0 < hp.MAX_HOSTS <= count:
                break

            if 0 < hp.TIMEOUT < (time.time() - start):
                raise Exception("Timeout exceeded")

            if hp.parseSSDPInfo(hp.recv(1024, server), False, False):
                count += 1

        except:
            print('\nDiscover mode halted..')
            break


def pcap(argc, argv, hp):
    """ Passively listen for UPNP NOTIFY packets """
    print('Entering passive mode, Ctl+C to stop...')
    print('')

    count = 0
    start = time.time()

    while True:
        try:
            if 0 < hp.MAX_HOSTS <= count:
                break

            if 0 < hp.TIMEOUT < (time.time() - start):
                raise Exception("Timeout exceeded")

            if hp.parseSSDPInfo(hp.recv(1024, False), False, False):
                count += 1

        except:
            print("\nPassive mode halted...")
            break


def head(argc, argv, hp):
    """ Manipulate M-SEARCH header values """
    if argc >= 2:
        action = argv[1]
        # Show current headers
        if action == 'show':
            for header, value in hp.msearchHeaders.items():
                print(header, ':', value)
            return
        # Delete the specified header
        elif action == 'del':
            if argc == 3:
                header = argv[2]
                if header in hp.msearchHeaders.keys():
                    del hp.msearchHeaders[header]
                    print('%s removed from header list' % header)
                    return
                else:
                    print('%s is not in the current header list' % header)
                    return
        # Create/set a headers
        elif action == 'set':
            if argc == 4:
                header = argv[2]
                value = argv[3]
                hp.msearchHeaders[header] = value
                print("Added header: '%s:%s" % (header, value))
                return

    showHelp(argv[0])


def set(argc, argv, hp):
    """ Manipulate application settings """
    if argc >= 2:
        action = argv[1]
        if action == 'uniq':
            hp.UNIQ = toggleVal(hp.UNIQ)
            print("Show unique hosts set to: %s" % hp.UNIQ)
            return
        elif action == 'debug':
            hp.DEBUG = toggleVal(hp.DEBUG)
            print("Debug mode set to: %s" % hp.DEBUG)
            return
        elif action == 'verbose':
            hp.VERBOSE = toggleVal(hp.VERBOSE)
            print("Verbose mode set to: %s" % hp.VERBOSE)
            return
        elif action == 'version':
            if argc == 3:
                hp.UPNP_VERSION = argv[2]
                print('UPNP version set to: %s' % hp.UPNP_VERSION)
            else:
                showHelp(argv[0])
            return
        elif action == 'iface':
            if argc == 3:
                hp.IFACE = argv[2]
                print('Interface set to %s, re-binding sockets...' % hp.IFACE)
                if hp.initSockets(hp.ip, hp.port, hp.IFACE):
                    print('Interface change successful!')
                else:
                    print('Failed to bind new interface - are you sure you have root privilages??')
                    hp.IFACE = None
                return
        elif action == 'socket':
            if argc == 3:
                try:
                    (ip, port) = argv[2].split(':')
                    port = int(port)
                    hp.ip = ip
                    hp.port = port
                    hp.cleanup()
                    if not hp.initSockets(ip, port, hp.IFACE):
                        print("Setting new socket %s:%d failed!" % (ip, port))
                    else:
                        print("Using new socket: %s:%d" % (ip, port))
                except Exception as e:
                    print('Caught exception setting new socket:', e)
                return
        elif action == 'timeout':
            if argc == 3:
                try:
                    hp.TIMEOUT = int(argv[2])
                except Exception as e:
                    print('Caught exception setting new timeout value:', e)
                return
        elif action == 'max':
            if argc == 3:
                try:
                    hp.MAX_HOSTS = int(argv[2])
                except Exception as e:
                    print('Caught exception setting new max host value:', e)
                return
        elif action == 'show':
            print('Multicast IP:          ', hp.ip)
            print('Multicast port:        ', hp.port)
            print('Network interface:     ', hp.IFACE)
            print('Receive timeout:       ', hp.TIMEOUT)
            print('Host discovery limit:  ', hp.MAX_HOSTS)
            print('Number of known hosts: ', len(hp.ENUM_HOSTS))
            print('UPNP version:          ', hp.UPNP_VERSION)
            print('Debug mode:            ', hp.DEBUG)
            print('Verbose mode:          ', hp.VERBOSE)
            print('Show only unique hosts:', hp.UNIQ)
            print('Using log file:        ', hp.LOG_FILE)
            return

    showHelp(argv[0])
    return


def host(argc, argv, hp):
    """ Host command. It's kind of big. """
    hostInfo = None
    indexList = []
    indexError = "Host index out of range. Try the 'host list' command to get a list of known hosts"

    if argc >= 2:
        action = argv[1]
        if action == 'list':
            if len(hp.ENUM_HOSTS) == 0:
                print("No known hosts - try running the 'msearch' or 'pcap' commands")
                return
            for index, hostInfo in hp.ENUM_HOSTS.items():
                print("\t[%d] %s" % (index, hostInfo['name']))
            return
        elif action == 'details':
            if argc == 3:
                try:
                    index = int(argv[2])
                    hostInfo = hp.ENUM_HOSTS[index]
                except Exception:
                    print(indexError)
                    return

                try:
                    # If this host data is already complete, just display it
                    if hostInfo['dataComplete']:
                        hp.showCompleteHostInfo(index, False)
                    else:
                        print("Can't show host info because I don't have it. Please run 'host get %d'" % index)
                except KeyboardInterrupt as e:
                    print("")
                    pass
                return

        elif action == 'summary':
            if argc == 3:

                try:
                    index = int(argv[2])
                    hostInfo = hp.ENUM_HOSTS[index]
                except:
                    print(indexError)
                    return

                print('Host:', hostInfo['name'])
                print('XML File:', hostInfo['xmlFile'])
                for deviceName, deviceData in hostInfo['deviceList'].items():
                    print(deviceName)
                    for k, v in deviceData.items():
                        try:
                            v.has_key(False)
                        except:
                            print("\t%s: %s" % (k, v))
                print('')
                return

        elif action == 'info':
            output = hp.ENUM_HOSTS
            dataStructs = []
            for arg in argv[2:]:
                try:
                    arg = int(arg)
                except:
                    pass
                if arg not in output.keys():
                    print('Invalid property', arg)
                    return
                output = output.get(arg)
            try:
                for k, v in output.items():
                    try:
                        v.has_key(False)
                        dataStructs.append(k)
                    except:
                        print(k, ':', v)
                        continue
            except:
                print(output)

            for struct in dataStructs:
                print(struct, ': {}')
            return

        elif action == 'get':
            if argc == 3:
                try:
                    index = int(argv[2])
                    hostInfo = hp.ENUM_HOSTS[index]
                except:
                    print(indexError)
                    return

                if hostInfo:
                    # If this host data is already complete, just display it
                    if hostInfo['dataComplete']:
                        print('Data for this host has already been enumerated!')
                        return

                    try:
                        # Get extended device and service information
                        if hostInfo:
                            print("Requesting device and service info for %s (this could take a few seconds)..." %
                                  hostInfo['name'])
                            print('')
                            if not hostInfo['dataComplete']:
                                (xmlHeaders, xmlData) = hp.getXML(hostInfo['xmlFile'])
                                if not xmlData:
                                    print('Failed to request host XML file:', hostInfo['xmlFile'])
                                    return
                                if not hp.getHostInfo(xmlData, xmlHeaders, index):
                                    print("Failed to get device/service info for %s..." % hostInfo['name'])
                                    return
                            print('Host data enumeration complete!')
                            hp.updateCmdCompleter(hp.ENUM_HOSTS)
                            return
                    except KeyboardInterrupt as e:
                        print("")
                        return

        elif action == 'send':
            # Send SOAP requests
            index = False
            inArgCounter = 0

            if argc != 6:
                showHelp(argv[0])
                return
            else:
                try:
                    index = int(argv[2])
                    hostInfo = hp.ENUM_HOSTS[index]
                except:
                    print(indexError)
                    return
                deviceName = argv[3]
                serviceName = argv[4]
                actionName = argv[5]
                actionArgs = False
                sendArgs = {}
                retTags = []
                controlURL = False
                fullServiceName = False

                # Get the service control URL and full service name
                try:
                    controlURL = hostInfo['proto'] + hostInfo['name']
                    controlURL2 = hostInfo['deviceList'][deviceName]['services'][serviceName]['controlURL']
                    if not controlURL.endswith('/') and not controlURL2.startswith('/'):
                        controlURL += '/'
                    controlURL += controlURL2
                except Exception as e:
                    print('Caught exception:', e)
                    print("Are you sure you've run 'host get %d' and specified the correct service name?" % index)
                    return False

                # Get action info
                try:
                    actionArgs = hostInfo['deviceList'][deviceName]['services'][serviceName]['actions'][actionName][
                        'arguments']
                    fullServiceName = hostInfo['deviceList'][deviceName]['services'][serviceName]['fullName']
                except Exception as e:
                    print('Caught exception:', e)
                    print("Are you sure you've specified the correct action?")
                    return False

                for argName, argVals in actionArgs.items():
                    actionStateVar = argVals['relatedStateVariable']
                    stateVar = hostInfo['deviceList'][deviceName]['services'][serviceName]['serviceStateVariables'][
                        actionStateVar]

                    if argVals['direction'].lower() == 'in':
                        print("Required argument:")
                        print("\tArgument Name: ", argName)
                        print("\tData Type:     ", stateVar['dataType'])
                        if 'allowedValueList' in stateVar.keys():
                            print("\tAllowed Values:", stateVar['allowedValueList'])
                        if 'allowedValueRange' in stateVar.keys():
                            print("\tValue Min:     ", stateVar['allowedValueRange'][0])
                            print("\tValue Max:     ", stateVar['allowedValueRange'][1])
                        if 'defaultValue' in stateVar.keys():
                            print("\tDefault Value: ", stateVar['defaultValue'])
                        prompt = "\tSet %s value to: " % argName
                        try:
                            # Get user input for the argument value
                            (argc, argv) = getUserInput(hp, prompt)
                            if not argv:
                                print('Stopping send request...')
                                return
                            uInput = ''

                            if argc > 0:
                                inArgCounter += 1

                            for val in argv:
                                uInput += val + ' '

                            uInput = uInput.strip()
                            if stateVar['dataType'] == 'bin.base64' and uInput:
                                uInput = base64.b64encode(uInput.encode('utf-8'))

                            sendArgs[argName] = (uInput.strip(), stateVar['dataType'])
                        except KeyboardInterrupt:
                            print("")
                            return
                        print('')
                    else:
                        retTags.append((argName, stateVar['dataType']))

                # Remove the above inputs from the command history
                while inArgCounter:
                    try:
                        readline.remove_history_item(readline.get_current_history_length() - 1)
                    except:
                        pass

                    inArgCounter -= 1

                # print('Requesting',controlURL)
                soapResponse = hp.sendSOAP(hostInfo['name'], fullServiceName, controlURL, actionName, sendArgs)
                if soapResponse:
                    # It's easier to just parse this ourselves...
                    for (tag, dataType) in retTags:
                        tagValue = hp.extractSingleTag(soapResponse, tag)
                        if dataType == 'bin.base64' and tagValue:
                            tagValue = base64.b64decode(tagValue)
                        print(tag, ':', tagValue)
            return

    showHelp(argv[0])
    return


def save(argc, argv, hp):
    """ Save data """
    suffix = '%s_%s.mir'
    uniqName = ''
    saveType = ''
    fnameIndex = 3

    if argc >= 2:
        idx = 0
        if argv[1] == 'help':
            showHelp(argv[0])
            return
        elif argv[1] == 'data':
            saveType = 'struct'
            if argc == 3:
                idx = argv[2]
            else:
                idx = 'data'
        elif argv[1] == 'info':
            saveType = 'info'
            fnameIndex = 4
            if argc >= 3:
                try:
                    idx = int(argv[2])
                except:
                    print('Host index is not a number!')
                    showHelp(argv[0])
                    return
            else:
                showHelp(argv[0])
                return

        if argc == fnameIndex:
            uniqName = argv[fnameIndex - 1]
        else:
            uniqName = idx
    else:
        showHelp(argv[0])
        return

    fileName = suffix % (saveType, uniqName)
    if os.path.exists(fileName):
        print("File '%s' already exists! Please try again..." % fileName)
        return
    if saveType == 'struct':
        try:
            with open(fileName, 'wb') as fp:
                # noinspection PyTypeChecker
                pickle.dump(hp.ENUM_HOSTS, fp)
                print("Host data saved to '%s'" % fileName)
        except Exception as e:
            print('Caught exception saving host data:', e)
    elif saveType == 'info':
        try:
            with open(fileName, 'w') as fp:
                hp.showCompleteHostInfo(index, fp)
                print("Host info for '%s' saved to '%s'" % (hp.ENUM_HOSTS[index]['name'], fileName))
        except Exception as e:
            print('Failed to save host info:', e)
            return
    else:
        showHelp(argv[0])

    return


def load(argc, argv, hp):
    """ Load data """
    if argc == 2 and argv[1] != 'help':
        loadFile = argv[1]

        try:
            with open(loadFile, 'rb') as fp:
                hp.ENUM_HOSTS = {}
                hp.ENUM_HOSTS = pickle.load(fp)
            hp.updateCmdCompleter(hp.ENUM_HOSTS)
            print('Host data restored:')
            print('')
            host(2, ['host', 'list'], hp)
            return
        except Exception as e:
            print('Caught exception while restoring host data:', e)

    showHelp(argv[0])


def log(argc, argv, hp):
    """ Open log file """
    if argc == 2:
        logFile = argv[1]
        try:
            fp = open(logFile, 'a')
        except Exception as e:
            print('Failed to open %s for logging: %s' % (logFile, e))
            return
        try:
            hp.LOG_FILE = fp
            theTime = time.strftime("%d-%m-%Y, %H:%M:%S", time.localtime())
            hp.LOG_FILE.write("\n### Logging started at: %s ###\n" % theTime)
        except Exception as e:
            print("Cannot write to file '%s': %s" % (logFile, e))
            hp.LOG_FILE = False
            return
        print("Commands will be logged to: '%s'" % logFile)
        return
    showHelp(argv[0])


def help(argc, argv, hp):
    """ Show help """
    showHelp(False)


def debug(argc, argv, hp):
    """ Debug, disabled by default """
    command = ''
    if not hp.DEBUG:
        print('Debug is disabled! To enable, try the set command...')
        return
    if argc == 1:
        showHelp(argv[0])
    else:
        for cmd in argv[1:]:
            command += cmd + ' '
        command = command.strip()
        print(eval(command))
    return


def exit(argc, argv, hp):
    """ quit """
    quit(argc, argv, hp)


def quit(argc, argv, hp):
    """ quit """
    if argc == 2 and argv[1] == 'help':
        showHelp(argv[0])
        return
    print('Bye!')
    print('')
    hp.cleanup()
    sys.exit(0)

################ End Action Functions ######################

def showHelp(command):
    """ Show command help """
    # Detailed help info for each command
    helpInfo = {
        'help': {
            'longListing':
                'Description:\n'
                '\tLists available commands and command descriptions\n\n'
                'Usage:\n'
                '\t%s\n'
                '\t<command> help',
            'quickView':
                'Show program help'
        },
        'quit': {
            'longListing':
                'Description:\n'
                '\tQuits the interactive shell\n\n'
                'Usage:\n'
                '\t%s',
            'quickView':
                'Exit this shell'
        },
        'exit': {

            'longListing':
                'Description:\n'
                '\tExits the interactive shell\n\n'
                'Usage:\n'
                '\t%s',
            'quickView':
                'Exit this shell'
        },
        'save': {
            'longListing':
                'Description:\n'
                '\tSaves current host information to disk.\n\n'
                'Usage:\n'
                '\t%s <data | info <host#>> [file prefix]\n'
                "\tSpecifying 'data' will save the raw host data to a file suitable for importing later via 'load'\n"
                "\tSpecifying 'info' will save data for the specified host in a human-readable format\n"
                "\tSpecifying a file prefix will save files in for format of 'struct_[prefix].mir' and info_[prefix].mir\n\n"
                'Example:\n'
                '\t> save data wrt54g\n'
                '\t> save info 0 wrt54g\n\n'
                'Notes:\n'
                "\to Data files are saved as 'struct_[prefix].mir'; info files are saved as 'info_[prefix].mir.'\n"
                "\to If no prefix is specified, the host index number will be used for the prefix.\n"
                "\to The data saved by the 'save info' command is the same as the output of the 'host details' command.",
            'quickView':
                'Save current host data to file'
        },
        'set': {
            'longListing':
                'Description:\n'
                '\tAllows you  to view and edit application settings.\n\n'
                'Usage:\n'
                '\t%s <show | uniq | debug | verbose | version <version #> | iface <interface> | socket <ip:port> | timeout <seconds> | max <count> >\n'
                "\t'show' displays the current program settings\n"
                "\t'uniq' toggles the show-only-uniq-hosts setting when discovering UPNP devices\n"
                "\t'debug' toggles debug mode\n"
                "\t'verbose' toggles verbose mode\n"
                "\t'version' changes the UPNP version used\n"
                "\t'iface' changes the network interface in use\n"
                "\t'socket' re-sets the multicast IP address and port number used for UPNP discovery\n"
                "\t'timeout' sets the receive timeout period for the msearch and pcap commands (default: infinite)\n"
                "\t'max' sets the maximum number of hosts to locate during msearch and pcap discovery modes\n\n"
                'Example:\n'
                '\t> set socket 239.255.255.250:1900\n'
                '\t> set uniq\n\n'
                'Notes:\n'
                "\tIf given no options, 'set' will display help options",
            'quickView':
                'Show/define application settings'
        },
        'head': {
            'longListing':
                'Description:\n'
                '\tAllows you to view, set, add and delete the SSDP header values used in SSDP transactions\n\n'
                'Usage:\n'
                '\t%s <show | del <header> | set <header>  <value>>\n'
                "\t'set' allows you to set SSDP headers used when sending M-SEARCH queries with the 'msearch' command\n"
                "\t'del' deletes a current header from the list\n"
                "\t'show' displays all current header info\n\n"
                'Example:\n'
                '\t> head show\n'
                '\t> head set MX 3',
            'quickView':
                'Show/define SSDP headers'
        },
        'host': {
            'longListing':
                'Description:\n'
                "\tAllows you to query host information and iteract with a host's actions/services.\n\n"
                'Usage:\n'
                '\t%s <list | get | info | summary | details | send> [host index #]\n'
                "\t'list' displays an index of all known UPNP hosts along with their respective index numbers\n"
                "\t'get' gets detailed information about the specified host\n"
                "\t'details' gets and displays detailed information about the specified host\n"
                "\t'summary' displays a short summary describing the specified host\n"
                "\t'info' allows you to enumerate all elements of the hosts object\n"
                "\t'send' allows you to send SOAP requests to devices and services *\n\n"
                'Example:\n'
                '\t> host list\n'
                '\t> host get 0\n'
                '\t> host summary 0\n'
                '\t> host info 0 deviceList\n'
                '\t> host send 0 <device name> <service name> <action name>\n\n'
                'Notes:\n'
                "\to All host commands support full tab completion of enumerated arguments\n"
                "\to All host commands EXCEPT for the 'host send', 'host info' and 'host list' commands take only one argument: the host index number.\n"
                "\to The host index number can be obtained by running 'host list', which takes no futher arguments.\n"
                "\to The 'host send' command requires that you also specify the host's device name, service name, and action name that you wish to send,\n\t  in that order (see the last example in the Example section of this output). This information can be obtained by viewing the\n\t  'host details' listing, or by querying the host information via the 'host info' command.\n"
                "\to The 'host info' command allows you to selectively enumerate the host information data structure. All data elements and their\n\t  corresponding values are displayed; a value of '{}' indicates that the element is a sub-structure that can be further enumerated\n\t  (see the 'host info' example in the Example section of this output).",
            'quickView':
                'View and send host list and host information'
        },
        'pcap': {
            'longListing':
                'Description:\n'
                '\tPassively listens for SSDP NOTIFY messages from UPNP devices\n\n'
                'Usage:\n'
                '\t%s',
            'quickView':
                'Passively listen for UPNP hosts'
        },
        'msearch': {
            'longListing':
                'Description:\n'
                '\tActively searches for UPNP hosts using M-SEARCH queries\n\n'
                'Usage:\n'
                "\t%s [device | service] [<device name> | <service name>]\n"
                "\tIf no arguments are specified, 'msearch' searches for upnp:rootdevices\n"
                "\tSpecific device/services types can be searched for using the 'device' or 'service' arguments\n\n"
                'Example:\n'
                '\t> msearch\n'
                '\t> msearch service WANIPConnection\n'
                '\t> msearch device InternetGatewayDevice',
            'quickView':
                'Actively locate UPNP hosts'
        },
        'load': {
            'longListing':
                'Description:\n'
                "\tLoads host data from a struct file previously saved with the 'save data' command\n\n"
                'Usage:\n'
                '\t%s <file name>',
            'quickView':
                'Restore previous host data from file'
        },
        'log': {
            'longListing':
                'Description:\n'
                '\tLogs user-supplied commands to a log file\n\n'
                'Usage:\n'
                '\t%s <log file name>',
            'quickView':
                'Logs user-supplied commands to a log file'
        }
    }

    try:
        print(helpInfo[command]['longListing'] % command)
    except:
        for command, cmdHelp in helpInfo.items():
            print("%s\t\t%s" % (command, cmdHelp['quickView']))


def usage():
    """ Display usage """
    print('''
Command line usage: %s [OPTIONS]

    -s <struct file>    Load previous host data from struct file
    -l <log file>        Log user-supplied commands to log file
    -i <interface>        Specify the name of the interface to use (Linux only, requires root)
    -b <batch file>     Process commands from a file
    -u            Disable show-uniq-hosts-only option
    -d            Enable debug mode
    -v            Enable verbose mode
    -h             Show help
''' % sys.argv[0])
    sys.exit(1)


def parseCliOpts(argc, argv, hp):
    """ Check command line options """
    try:
        opts, args = getopt.getopt(argv[1:], 's:l:i:b:udvh')
    except getopt.GetoptError as e:
        print('Usage Error:', e)
        usage()
    else:
        for (opt, arg) in opts:
            if opt == '-s':
                print('')
                load(2, ['load', arg], hp)
                print('')
            elif opt == '-l':
                print('')
                log(2, ['log', arg], hp)
                print('')
            elif opt == '-u':
                hp.UNIQ = toggleVal(hp.UNIQ)
            elif opt == '-d':
                hp.DEBUG = toggleVal(hp.DEBUG)
                print('Debug mode enabled!')
            elif opt == '-v':
                hp.VERBOSE = toggleVal(hp.VERBOSE)
                print('Verbose mode enabled!')
            elif opt == '-b':
                hp.BATCH_FILE = open(arg, 'r')
                print("Processing commands from '%s'..." % arg)
            elif opt == '-h':
                usage()
            elif opt == '-i':
                networkInterfaces = []
                requestedInterface = arg
                interfaceName = None
                found = False

                # Search for the interface
                found = interface_exists(requestedInterface)
                if not found and len(networkInterfaces) > 0:
                    print("Failed to find interface '%s'; try one of these:\n" % requestedInterface)
                    for iface in networkInterfaces:
                        print(iface)
                    print('')
                    sys.exit(1)
                else:
                    if not hp.initSockets(False, False, interfaceName):
                        print('Binding to interface %s failed; are you sure you have root privilages??' % interfaceName)


def toggleVal(val):
    """ Toggle boolean values """
    if val:
        return False
    else:
        return True


def getUserInput(hp, shellPrompt):
    """ Prompt for user input """
    defaultShellPrompt = 'upnp> '

    if hp.BATCH_FILE:
        return getFileInput(hp)

    if not shellPrompt:
        shellPrompt = defaultShellPrompt

    try:
        uInput = input(shellPrompt).strip()
        argv = uInput.split()
        argc = len(argv)
    except KeyboardInterrupt:
        print('\n')
        if shellPrompt == defaultShellPrompt:
            quit(0, [], hp)
        return 0, None
    if hp.LOG_FILE:
        try:
            hp.LOG_FILE.write("%s\n" % uInput)
        except:
            print('Failed to log data to log file!')

    return argc, argv


def getFileInput(hp):
    """ Reads scripted commands from a file """
    data = False
    line = hp.BATCH_FILE.readline()
    if line:
        data = True
        line = line.strip()

    argv = line.split()
    argc = len(argv)

    if not data:
        hp.BATCH_FILE.close()
        hp.BATCH_FILE = None

    return argc, argv


def main(argc, argv):
    """ main """
    # Table of valid commands - all primary commands must have an associated function
    appCommands = {
        'help': {
            'help': None
        },
        'quit': {
            'help': None
        },
        'exit': {
            'help': None
        },
        'save': {
            'data': None,
            'info': None,
            'help': None
        },
        'load': {
            'help': None
        },
        'set': {
            'uniq': None,
            'socket': None,
            'show': None,
            'iface': None,
            'debug': None,
            'version': None,
            'verbose': None,
            'timeout': None,
            'max': None,
            'help': None
        },
        'head': {
            'set': None,
            'show': None,
            'del': None,
            'help': None
        },
        'host': {
            'list': None,
            'info': None,
            'get': None,
            'details': None,
            'send': None,
            'summary': None,
            'help': None
        },
        'pcap': {
            'help': None
        },
        'msearch': {
            'device': None,
            'service': None,
            'help': None
        },
        'log': {
            'help': None
        },
        'debug': {
            'command': None,
            'help': None
        }
    }

    # The load command should auto complete on the contents of the current directory
    for file in os.listdir(os.getcwd()):
        appCommands['load'][file] = None

    # Initialize upnp class
    hp = upnp(False, False, None, appCommands)

    # Set up tab completion and command history
    readline.parse_and_bind("tab: complete")
    readline.set_completer(hp.completer.complete)

    # Set some default values
    hp.UNIQ = True
    hp.VERBOSE = False
    action = False
    funPtr = False

    # Check command line options
    parseCliOpts(argc, argv, hp)

    # Main loop
    while True:
        # Drop user into shell
        if hp.BATCH_FILE:
            (argc, argv) = getFileInput(hp)
        else:
            (argc, argv) = getUserInput(hp, False)
        if argc == 0:
            continue
        action = argv[0]
        funcPtr = None

        print('')
        # Parse actions
        try:
            if action in appCommands.keys():
                funcPtr = eval(action)
        except:
            funcPtr = None
            action = False

        if callable(funcPtr):
            if argc == 2 and argv[1] == 'help':
                showHelp(argv[0])
            else:
                try:
                    funcPtr(argc, argv, hp)
                except KeyboardInterrupt:
                    print('\nAction interrupted by user...')
            print('')
            continue
        print('Invalid command. Valid commands are:')
        print('')
        showHelp(False)
        print('')


if __name__ == "__main__":
    try:
        print('')
        print('Miranda v1.3 + Python3 patch')
        print('The interactive UPnP client')
        print('Craig Heffner, http://www.devttys0.com')
        print('')
        main(len(sys.argv), sys.argv)
    except Exception as e:
        print('Caught main exception:', e)
        sys.exit(1)
