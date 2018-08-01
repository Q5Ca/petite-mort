#!/usr/bin/env python2
## 
## vita-petite-mort-deux.py -- glitch enable brom + run payload
##
## Copyright (C) 2018 Yifan Lu
##
## This software may be modified and distributed under the terms
## of the MIT license.  See the LICENSE file for details.
## 
from __future__ import print_function, division

import time
import logging
import os
import csv
from enum import IntEnum

import chipwhisperer as cw
import sys
from chipwhisperer.capture.scopes.cwhardware.ChipWhispererExtra import CWExtraSettings
from chipwhisperer.capture.targets.mmccapture_readers._base import MMCPacket
from chipwhisperer.capture.targets.MMCCapture import MMCCapture as cwtarget
from chipwhisperer.common.utils import pluginmanager
from chipwhisperer.capture.targets.simpleserial_readers.cwlite import SimpleSerial_ChipWhispererLite

# Params

CW_SYSCLK_FREQ = 96000000
VITA_CLK_FREQ = 12000000
P1_MIN_OFFSET = 20
P1_MAX_OFFSET = 1000
P1_MIN_WIDTH = 55
P1_MAX_WIDTH = 55
P1_OFFSET_STEP = 1
P1_WIDTH_STEP = 1
P2_MIN_OFFSET = 40813
P2_MAX_OFFSET = 40813
P2_MIN_WIDTH = 52
P2_MAX_WIDTH = 52
P2_OFFSET_STEP = 1
P2_WIDTH_STEP = 1
VITA_UART0_BAUD = 28985
TIME_RESET_HOLD = 0
TIME_POWER_HOLD = 5
GLITCH_FIND_TIMEOUT = 10
PAYLOAD_TIMEOUT = 100
VERBOSE = 1

class States(IntEnum):
    BOOT_STARTED = 0
    READ_MBR = 1
    READ_MBR_STATUS = 2
    UNEXPECTED_READ = 3
    UNEXPECTED_PACKET = 4
    LOADING_PAYLOAD = 5
    OVERFLOWED = 6
    RESTARTED = 7

logging.basicConfig(level=logging.WARN)
scope = cw.scope()
target = cw.target(scope, cwtarget)

# setup parameters needed for glitch the stm32f
scope.glitch.clk_src = 'clkgen'

scope.clock.clkgen_freq = VITA_CLK_FREQ
scope.io.tio1 = "serial_tx"
scope.io.tio2 = "serial_rx"

# setup MMC trigger to look for READ_SINGLE_BLOCK of 0x0 response

mmctrigger = scope.mmcTrigger
mmctrigger.setMatchCmd(True)
mmctrigger.setCmdIndex(MMCPacket.Cmd.READ_SINGLE_BLOCK.value)
mmctrigger.setDirection(2)
mmctrigger.setDataCompareOp(1)
mmctrigger.setTriggerData('0x0')
mmctrigger.setTriggerNext(True)

# get MMC output
mmc = target.mmc

# get serial console
ser_cons = pluginmanager.getPluginsInDictFromPackage("chipwhisperer.capture.targets.simpleserial_readers", True, False)
ser = ser_cons[SimpleSerial_ChipWhispererLite._name]
ser.con(scope)
ser.setBaud(VITA_UART0_BAUD)

# format output table
headers = ['num packets', 'width', 'offset', 'success']
#glitch_display = GlitchResultsDisplay(headers)

# set glitch parameters
# trigger glitches with external trigger
scope.glitch.trigger_src = 'ext_continuous'
scope.glitch.output = 'enable_only'
scope.io.hs2 = 'clkgen'

# enable trigger
scope.advancedSettings.cwEXTRA.setTriggerModule(CWExtraSettings.MODULE_MMCTRIGGER)
scope.advancedSettings.cwEXTRA.setTargetGlitchOut('A', True)

# setup secondary glitch
scope.vddglitch.clk_src = 'clkgen'
scope.vddglitch.trigger_src = 'ext_continuous'
scope.vddglitch.output = 'enable_only'
scope.advancedSettings.cwEXTRA.setVddTriggerModule(CWExtraSettings.MODULE_EDGE)
edgetrigger = scope.edgeTrigger
edgetrigger.setPin(True, edgetrigger.PIN_NRST)
edgetrigger.setPinMode(edgetrigger.MODE_OR)
edgetrigger.setEdgeStyle(edgetrigger.EDGE_RISING)
edgetrigger.setFilter(1)

# init
target.init()

class PetiteMort:
    queue = []

    # From https://gist.github.com/sbz/1080258
    def hexdump(self, src, offset, length=16):
        FILTER = ''.join([(len(repr(chr(x))) == 3) and chr(x) or '.' for x in range(256)])
        lines = []
        for c in xrange(0, len(src), length):
            chars = src[c:c+length]
            hex = ' '.join(["%02x" % ord(x) for x in chars])
            printable = ''.join(["%s" % ((ord(x) <= 127 and FILTER[ord(x)]) or '.') for x in chars])
            lines.append("%08x  %-*s  %s\n" % (offset + c, length*3, hex, printable))
        return ''.join(lines)

    def triggerPayload(self):
        for offset in xrange(P2_MIN_OFFSET, P2_MAX_OFFSET+1, P2_OFFSET_STEP):
            # set offset from trigger
            scope.glitch.ext_offset = offset
            for width in xrange(P2_MIN_WIDTH, P2_MAX_WIDTH+1, P2_WIDTH_STEP):
                print('phase 2: trying offset {}, width {}'.format(offset, width))

                # reset device
                scope.io.nrst = 'low'
                scope.glitch.repeat = width
                #scope.glitch.repeat = 1
                # flush the buffer
                time.sleep(TIME_RESET_HOLD)

                timeout = GLITCH_FIND_TIMEOUT
                # wait for target to finish
                state = States.BOOT_STARTED

                last_cnt = 0
                while mmc.count() > 0:
                    pkt = mmc.read()
                    last_cnt = pkt.num
                    if VERBOSE:
                        print(str(pkt))

                scope.io.nrst = 'disabled'
                timestamp = 0
                restarted = 0
                reads = 0
                while timeout > 0:
                    while mmc.count() > 0:
                        timeout = GLITCH_FIND_TIMEOUT
                        pkt = mmc.read()
                        if pkt.num < last_cnt:
                            timestamp = ((pkt.num + 0x10000 - last_cnt) * 0x100 * 1000.0) / CW_SYSCLK_FREQ
                        else:
                            timestamp = ((pkt.num - last_cnt) * 0x100 * 1000.0) / CW_SYSCLK_FREQ
                        last_cnt = pkt.num
                        print('[{:10.5f}ms] {}'.format(timestamp, str(pkt)))
                        if pkt.is_req:
                            if pkt.cmd == MMCPacket.Cmd.GO_IDLE_STATE:
                                restarted += 1
                            if pkt.cmd == MMCPacket.Cmd.READ_SINGLE_BLOCK:
                                reads += 1
                            if state == States.BOOT_STARTED:
                                if pkt.cmd == MMCPacket.Cmd.READ_SINGLE_BLOCK and pkt.content == 0x0:
                                    state = States.READ_MBR
                            elif state == States.READ_MBR:
                                if pkt.cmd == MMCPacket.Cmd.SEND_STATUS:
                                    state = States.READ_MBR_STATUS
                                elif pkt.cmd == MMCPacket.Cmd.GO_IDLE_STATE:
                                    state = States.RESTARTED
                                elif pkt.cmd == MMCPacket.Cmd.READ_SINGLE_BLOCK:
                                    state = States.UNEXPECTED_READ
                                else:
                                    state = States.UNEXPECTED_PACKET
                            elif state == States.READ_MBR_STATUS:
                                if pkt.cmd == MMCPacket.Cmd.GO_IDLE_STATE:
                                    state = States.RESTARTED
                                elif pkt.cmd == MMCPacket.Cmd.READ_SINGLE_BLOCK:
                                    if pkt.content == 0x8000:
                                        state = States.LOADING_PAYLOAD
                                    else:
                                        state = States.UNEXPECTED_READ
                                else:
                                    state = States.UNEXPECTED_PACKET
                            elif state == States.LOADING_PAYLOAD:
                                if pkt.content == 0x80E1:
                                    state = States.OVERFLOWED
                        if restarted > 10 or state == States.RESTARTED or state == States.OVERFLOWED:
                            timeout = -1
                            break
                    else:
                        time.sleep(0.1)
                        timeout -= 1

                # for table display purposes
                data = [offset, width, state, reads]
                print(data)
                #glitch_display.add_data(data)

                if state == States.OVERFLOWED:
                    return True
        return False

    def waitForData(self):
        print('Glitch successful, waiting for UART data...')
        timeout = PAYLOAD_TIMEOUT
        self.queue = []
        while timeout > 0:
            count = ser.inWaiting()
            while count > 0:
                timeout = PAYLOAD_TIMEOUT
                dat = ser.read(count, 0)
                self.queue.extend(dat)
                if len(self.queue) >= 16:
                    print(self.hexdump(self.queue[0:16], 0), end="")
                    s = sum([ord(x) for x in self.queue[4:16]])
                    if s == 0:
                        return False
                    else:
                        return True
                    timeout = -1
                    break
                count = ser.inWaiting()
            else:
                time.sleep(0.1)
                timeout -= 1
        raise RuntimeError('Timed out waiting for data.')

    def dumpPayload(self, path=None):
        f = None
        if path:
            print('Dumping to {}'.format(path))
            f = open(path, "wb")

        timeout = PAYLOAD_TIMEOUT
        offset = 0
        while offset < 0x1000:
            count = ser.inWaiting()
            while count > 0 and offset < 0x1000:
                timeout = PAYLOAD_TIMEOUT
                dat = ser.read(count, 0)
                self.queue.extend(dat)
                if f:
                    f.write(dat)
                    f.flush()
                if (not f or VERBOSE) and len(self.queue) >= 16:
                    print(self.hexdump(self.queue[0:16], offset), end="")
                    self.queue = self.queue[16:]
                offset += 16
                count = ser.inWaiting()
            else:
                time.sleep(0.1)
                timeout -= 1
            while VERBOSE and mmc.count() > 0:
                pkt = mmc.read()
                print(str(pkt))

        if f:
            f.close()

    def start(self):
        # power on and hold reset
        print('Waiting for Vita to power on...')
        scope.io.nrst = 'low'
        scope.io.nrst = 'disabled'
        scope.io.pdid = 'low'
        while mmc.count() == 0:
            pass
        scope.io.pdid = 'disabled'
        scope.io.nrst = 'low'

        for offset in xrange(P1_MIN_OFFSET, P1_MAX_OFFSET+1, P1_OFFSET_STEP):
            # set offset from trigger
            scope.vddglitch.ext_offset = offset
            for width in xrange(P1_MIN_WIDTH, P1_MAX_WIDTH+1, P1_WIDTH_STEP):
                print('phase 1: trying offset {}, width {}'.format(offset, width))
                scope.io.nrst = 'low'
                scope.vddglitch.repeat = width
                print('Clearing buffer...')
                while ser.inWaiting() > 0:
                    ser.read(ser.inWaiting())
                print('Running payload trigger loop...')
                while not self.triggerPayload():
                    print('Trying again to trigger payload...')
                if self.waitForData():
                    self.dumpPayload('dumprom.bin')
                    print('Maybe this is bootrom?')
                else:
                    print('Failed to see bootrom')
        return False

PetiteMort().start()
