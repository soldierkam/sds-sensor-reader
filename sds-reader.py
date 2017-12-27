import serial
import os 
import sys, time
from datetime import datetime, timedelta
import commands
import subprocess
import json
import httplib, urllib
import tempfile
import pickle
import glob
import numpy as np


SENSORID = "YOUR-SENSOR-ID"
USBPORT  = "/dev/ttyUSB0"


class SDS011Reader:

    def __init__(self, inport):
        self.id = 0xFFFF
        self.serial = serial.Serial(port=inport,baudrate=9600)
        #self.readQueryMode()
        self.setInterval(5)
        self.setModeActive(True)
        
    def readQueryMode(self):
        packet = self.prepareCmd(0x2, 0x0)
        self.writePacket(packet)
        
    def setModeActive(self, active):
        packet = self.prepareCmd(0x2, 0x1)
        packet[4] = 0x00 if active else 0x01
        self.writePacket(packet)
        
    def setDeviceId(self, id):
        packet = self.prepareCmd(0x5, 0x0)
        packet[13] = id >> 8
        packet[14] = id & 0xFF
        self.writePacket(packet)    
    
    def setInterval(self, minutes):
        if minutes > 30:
            raise ValueError("Interval should be < 30")
        packet = self.prepareCmd(0x8, 0x1)
        packet[4] = minutes
        self.writePacket(packet)
        
    def prepareCmd(self, action, set_op):
        bytes = [0] * 19
        bytes[0] = 0xaa
        bytes[1] = 0xb4
        bytes[2] = action & 0xFF
        bytes[3] = set_op & 0xFF
        bytes[15] = self.id >> 8
        bytes[16] = self.id & 0xFF
        bytes[18] = 0xab
        return bytes
    
    def packetToStr(self, packet):
        return ":".join("{:02x}".format(c) for c in packet)
    
    def writePacket(self, packet):
        chksum = 0
        for v in packet[2:17]:
            chksum += v
            packet[17] = chksum & 0xFF
        to_send = ''.join(chr(x) for x in packet)
        print "writing " + self.packetToStr(packet)
        self.serial.write(to_send)

    def readSensorPacket(self):
        pck_len = 10
        packet = [0] * pck_len
        read = 0
        while read < pck_len:
            #toread = self.serial.inWaiting()
            #if toread == 0:
            #    time.sleep(0.01)
            #    continue
            v = ord(self.serial.read())
            print "{:02x}".format(v)
            packet[read] = v
            read += 1
            if read == 1 and packet[0] != 0xAA:
                packet = [0] * pck_len
                read = 0
            if read == 2 and packet[1] not in (0xC0, 0xC5):
                packet = [0] * pck_len
                read = 0
        return packet                

    def readValue( self ):
        while True:
            packet = self.readSensorPacket()
            print "received " + self.packetToStr(packet)
            if packet[1] != 0xC0:
                print "Wrong type: tring again"
                continue
            pm25 = (packet[2] + 256 * packet[3]) / 10
            pm10 = (packet[4] + 256 * packet[5]) / 10
            return [pm25, pm10]
        
    def read( self, duration ):
        start = os.times()[4]

        count = 0
        species = [[],[]]
        speciesType = ["pm2.5-mg","pm10-mg"]

        while os.times()[4] < start+duration:
            try:
                values = self.readValue()
                species[0].append(values[0])
                species[1].append(values[1])
                count += 1
                dt = os.times()[4]-start
                print("[{:4.1f}] Samples:{:2d} PM2.5:{:4d} PM10:{:4d} StdDev(PM2.5):{:3.1f}".format(
                    dt,count,values[0],values[1],np.std(species[0])
                    ))
                time.sleep(1)
            except KeyboardInterrupt:
                print "Bye"
                sys.exit()
            except:
                e = sys.exc_info()[0]
                print("Can not read the sensor data: "+str(e))

        values = []
        for i in range(len(species)):
            values.append( dict( 
                stddev = np.std(species[i]), 
                median = np.median(species[i]),
                min    = np.min(species[i]),
                max    = np.max(species[i]),
                avg    = np.average(species[i]),
                type   = speciesType[i],
                time   = datetime.now().isoformat(),
                sensor = "SDS",
                scale  = 1
                ))

        return values


class SensorDataUploader:

    def __init__(self, id):
        self.faildate = 0
        self.writecnt = 0
        self.id = id

    def httpPost(self,idata):
        try:
            postdata = dict(data=dict( 
                id=self.id, 
                data = idata,
                swver = "python-sensor-uploader/1.0",
                ))
            postdata = urllib.urlencode(postdata)
            headers = {"Content-type": "application/x-www-form-urlencoded", "Accept": "text/plain"}
            conn = httplib.HTTPConnection("sensor.aqicn.org")
            conn.request("POST", "/sensor/upload/", postdata, headers)
            response = conn.getresponse()
            data = response.read()
            print("Posting {2} bytes -> {0} {1} ".format(response.status, response.reason, len(postdata)))
            conn.close()
            djson = json.loads(data)
            r = djson["result"] == "ok"
            if r!=1:
                print("Server says -> {0} ".format(data))
            return r
        except:
            e = sys.exc_info()[0]
            print("http-post: error. "+str(e))
            return 0 


    def file_get_contents(self,filename):
        with open(filename) as f:
            return f.read()

    def file_put_contents(self,filename,data):
        with open(filename,'w') as f:
            f.write(data)
            f.close()

    def postValues(self,values):
        filePath = os.path.join(tempfile.gettempdir(), self.id+".pickle")
        if self.faildate==0:
            self.faildate = time.strftime("%Y-%m-%d-%H-%M-%S")
        filePath2 = os.path.join(os.path.dirname(__file__), "pending."+self.id+"."+self.faildate+".pickle")

        if os.path.isfile(filePath) and os.path.getsize(filePath)>0:
            ovalues = pickle.loads(self.file_get_contents(filePath))
            print(ovalues)

            print("previous queue size has "+str(len(ovalues))+" entries")
            values = ovalues + values
            os.remove(filePath)
            print(values)

        if not self.httpPost(values):
            n = len(values)
            print("upload not ok... there are now {0} entries pending ({1}).".format(n,filePath))
            self.file_put_contents(filePath,pickle.dumps(values))

            if n>15:
                self.writecnt +=1
                if self.writecnt>10:
                    #only write every 10 times to prevent from wearing the flash
                    self.file_put_contents(filePath2,pickle.dumps(values))
                    print("Writing to persistent storage: "+filePath2)
                    self.writecnt = 0

            if n>100:
                print("Persitent storage file is too big ({}) -- reseting to new file ".format(n))
                self.faildate = 0
        else:
            print("Data posting ok!")
            self.faildate = 0
            if os.path.isfile(filePath2):
                print("Deleting persistent storage file "+filePath2)
                os.remove(filePath2)
            self.uploadQueue()

    def uploadQueue(self):
            path = os.path.dirname(os.path.abspath(__file__))
            for file in glob.glob(path+'/*.pickle'):
                    print "Uploading {0}: ".format(file),
                    values = pickle.loads(self.file_get_contents(file))
                    if self.httpPost(values):
                            print("ok!")
                            os.remove(file)
                    else:
                            print("nope...")
            print("upload queue("+path+"): all done.")




def loop(usbport):
    print("Starting reading sensor "+SENSORID+" on port "+usbport)
    reader = SDS011Reader(usbport) 
    uploader = SensorDataUploader(SENSORID) 
    while 1:
	print "Reading.."
	values = reader.readValue()
	print "Values: " + str(values)
	#print "Posting.."
        #uploader.postValues(values)


if len(sys.argv)==2:
    loop(sys.argv[1])
else:
    loop(USBPORT)


