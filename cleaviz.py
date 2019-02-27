import log
logger = log.get_logger(__name__)

import channelconverter as chconv
import socket
import struct
import pyqtgraph as pg
import scipy.signal
import datetime
import json
import numpy as np
import threading
import time
import fcntl, termios, array


# Some important info for the visualizer is how the TCP buffers
# work. /proc/sys/net will give info about e.g. the maximum amount of
# buffered data in a TCP buffer, before you will start losing data. It
# is not suggested to increase the size for our purpose, but rather
# make sure that we are not completely filling buffers by drawing fast
# enough. If we are lagging too far behind the data we are receiving
# (FIONREAD is used to determine this), we should start warning so
# that parameters may be tuned.


def sync_watchdog(s, sample_rate):
    # Used to see how much data is ready to be read in from the socket
    # (to see if we are lagging behind).
    sock_size = array.array('i', [0])

    # After every segment, check if we still have data to be
    # received -- this signifies that we are not keeping up
    # with the data stream.
    while True:
        fcntl.ioctl(s, termios.FIONREAD, sock_size)
        if (sock_size[0] // 4 >= 20*sample_rate):
            logger.info('{s} data available in TCP socket'.format(s=sock_size[0]))
            logger.info('Cleaviz is struggling to keep up with the data rate')
        time.sleep(1)


def mcs_lookup(row, col):
    lookup = int(str(row) + str(col))
    if lookup in chconv.MCSChannelConverter.mcsviz_to_channel:
        return chconv.MCSChannelConverter.mcsviz_to_channel[lookup]
    else:
        return -1


def downsample(x, ds):
    n = len(x) // ds
    new1 = np.empty((n, 2))
    new2 = np.array(x[:n*ds]).reshape((n, ds))
    new1[:, 0] = new2.max(axis=1)
    new1[:, 1] = new2.min(axis=1)
    x = new1.reshape(n*2)
    return x


def init_plots(win, rows, cols):
    plots = [None] * 60
    plot_objects = [None] * 60
    for i in range(rows):
        for j in range(cols):
            # Special cases for edges that should be empty.
            if (i, j) in [(0, 0), (0, 7), (7, 0), (7, 7)]:
                continue
            channel = mcs_lookup(i+1, j+1)
            plots[channel] = win.addPlot(row=i, col=j)
            plots[channel].setYRange(-win.current_yrange, win.current_yrange, padding=0)
            plots[channel].hideAxis('left')
            plots[channel].hideAxis('bottom')
            plot_objects[channel] = plots[channel]
            plots[channel] = plots[channel].plot(pen=pg.mkPen('#EB9904'), clear=True)
    return plots, plot_objects


class CleavizWindow(pg.GraphicsWindow):
    sig_key_press = pg.Qt.QtCore.pyqtSignal(object)

    def __init__(self, sample_rate, segment_length, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Hard code these for now, use argparse maybe later? We are just
        # testing that we are receiving something at all, really.
        self.address = 'localhost'
        self.port = 8080

        # Cosmetic.
        self.current_yrange = 10**(-4)
        self.setBackground("#353535")
        self.setFixedSize(1200, 800)

        # Hyperparameters for visualization.
        self.sample_rate = sample_rate
        self.segment_length = segment_length
        self.seconds = 3
        self.data_in_window = self.sample_rate*self.seconds // 10

        # Initialize the plots of the main window.
        self.rows = 8
        self.cols = 8
        self.plots, self.plot_objects = init_plots(self, self.rows, self.cols)
        self.zoomed_plot = None
        self.zoomed_plot_num = None

        # Connect mouse/key signals to respective handlers.
        self.scene().sigMouseClicked.connect(self.on_click)
        self.keyPressEvent = self.on_key_press

        self.x_axis_data = np.arange(self.data_in_window)


    def recv_segment(self):
        bytes_received = 0
        segment_data = bytearray(b'')

        for current_channel in range(60):
            # We are receiving 4-byte floats.
            data = self.s.recv(self.segment_length*4 - bytes_received)
            bytes_received = bytes_received + len(data)
            segment_data = np.append(segment_data, data)

            if (bytes_received != self.segment_length*4):
                continue

            # Print the received segment data.
            new_channel_data = []

            for i in struct.iter_unpack('f', segment_data):
                new_channel_data.append(i[0])
            new_channel_data = downsample(new_channel_data, 20)

            self.channel_data[current_channel] = np.append(self.channel_data[current_channel], new_channel_data)
            self.channel_data[current_channel] = self.channel_data[current_channel][-self.data_in_window:]

            # Reset for next segment.
            segment_data = bytearray(b'')
            bytes_received = 0


    def update_plots(self):
        # Data to plot.
        x_axis_data = self.x_axis_data[:len(self.channel_data[0])]

        if self.zoomed_plot:
            self.zoomed_plot.plot(x_axis_data, self.channel_data[self.zoomed_plot_num],
                                  pen=pg.mkPen('#EB9904'), clear=True)
            pg.QtGui.QApplication.processEvents()
        else:
            for i in range(self.rows):
                for j in range(self.cols):
                    channel = mcs_lookup(i+1, j+1)
                    if channel != -1:
                        self.plots[channel].setData(x=x_axis_data, y=self.channel_data[channel])
            pg.QtGui.QApplication.processEvents()


    def run(self):
        self.channel_data = {n: np.array([]) for n in range(60)}
        segment_counter = 0

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as self.s:
            self.s.connect((self.address, self.port))

            # Are we struggling to keep up with the data stream?.
            threading.Thread(target=sync_watchdog, args=([self.s, self.sample_rate])).start()

            while True:
                self.recv_segment()
                segment_counter = (segment_counter + 1) % (1000 // self.segment_length - 0)
                if segment_counter == 0:
                    self.update_plots()


    def on_click(self, event):
        # Ignore clicks that are not left-clicks.
        if event.button() != 1:
            return

        if self.zoomed_plot:
            self.zoomed_plot = None

            # Go back to plotting all channels.
            self.clear()
            self.plots, self.plot_objects = init_plots(self, self.rows, self.cols)
        else:
            clicked_items = self.scene().items(event.scenePos())
            self.zoomed_plot = [x for x in clicked_items if isinstance(x, pg.PlotItem)][0]
            x_axis = self.zoomed_plot.items[0].xData
            y_axis = self.zoomed_plot.items[0].yData
            for i, plot in enumerate(self.plots):
                if np.array_equal(self.zoomed_plot.items[0].yData, plot.yData):
                    self.zoomed_plot_num = i

            # Add a new, singular plot to the window (zoomed in).
            self.clear()
            self.zoomed_plot = self.addPlot()
            self.zoomed_plot.plot(x_axis, y_axis, pen=pg.mkPen('#EB9904'), clear=True)
            self.zoomed_plot.setYRange(-self.current_yrange, self.current_yrange, padding=0)


    def on_key_press(self, event):
        if event.text() == 'j':
            self.current_yrange /= 2
        elif event.text() == 'k':
            self.current_yrange *= 2

        if self.zoomed_plot:
            self.zoomed_plot.setYRange(-self.current_yrange, self.current_yrange, padding=0)
        else:
            for plot in self.plot_objects:
                plot.setYRange(-self.current_yrange, self.current_yrange, padding=0)


def main():
    app = pg.QtGui.QApplication([])
    win = CleavizWindow(sample_rate=10000, segment_length=100)
    win.run()


if __name__ == '__main__':
    main()
