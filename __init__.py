# The MIT License (MIT)
#
# Copyright (c) 2016 Ethan Ward
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import sys
import subprocess
import json
import time
from os import makedirs, remove, listdir, path
from os.path import dirname, join, exists, expanduser, isfile, abspath, isdir
import shutil
from adapt.intent import IntentBuilder
from mycroft.skills.core import MycroftSkill, intent_handler
from mycroft.util.log import LOG
from fuzzywuzzy import fuzz, process as fuzz_process

from mycroft.audio import wait_while_speaking
from mycroft.messagebus.message import Message
import mycroft.client.enclosure.display_manager as DisplayManager


class PianobarSkill(MycroftSkill):
    def __init__(self):
        super(PianobarSkill, self).__init__(name="PianobarSkill")
        self.process = None
        self.piano_bar_state = None  # 'playing', 'paused', 'autopause'
        self.current_station = None
        self._is_setup = False
        self.vocabs = []    # keep a list of vocabulary words
        self.pianobar_path = expanduser('~/.config/pianobar')
        self._pianobar_initated = False
        self.debug_mode = False
        self.idle_count = 0

        # Initialize settings values
        self.settings["email"] = ""
        self.settings["password"] = ""
        self.settings["song_artist"] = ""
        self.settings["song_title"] = ""
        self.settings["song_album"] = ""
        self.settings["station_name"] = ""
        self.settings["station_count"] = 0
        self.settings["stations"] = []
        self.settings["last_played"] = None
        self.settings['first_init'] = True  # True = first run ever

    def initialize(self):
        self._load_vocab_files()
        self._init_pianobar()

        self.schedule_repeating_event(self._poll_for_pianobar_update,
                                      None, 1,
                                      name='MonitorPianobar')
        self.add_event('recognizer_loop:record_begin',
                       self.handle_listener_started)

        # Monitor for credential changes
        self.settings.set_changed_callback(self.on_websettings_changed)
        self.on_websettings_changed()

    def get_intro_message(self):
        # This will be spoken on first installation
        if not self._is_setup:
            return self.translate("please.register.pandora")
        else:
            return None

    ######################################################################
    # 'Auto ducking' - pause playback when Mycroft wakes

    def handle_listener_started(self, message):
        if self.piano_bar_state == "playing":
            self.handle_pause()
            self.piano_bar_state = "autopause"

            # Start idle check
            self.idle_count = 0
            self.cancel_scheduled_event('IdleCheck')
            self.schedule_repeating_event(self.check_for_idle, None,
                                          1, name='IdleCheck')

    def check_for_idle(self):
        if not self.piano_bar_state == "autopause":
            self.cancel_scheduled_event('IdleCheck')
            return

        if DisplayManager.get_active() == '':
            # No activity, start to fall asleep
            self.idle_count += 1

            if self.idle_count >= 2:
                # Resume playback after 2 seconds of being idle
                self.cancel_scheduled_event('IdleCheck')
                self.handle_resume_song()
        else:
            self.idle_count = 0

    ######################################################################

    def _register_all_intents(self):
        """ Intents should only be registered once settings are inputed
            to avoid conflicts with other music skills
        """
        next_song_intent = IntentBuilder("PandoraNextIntent"). \
            require("Next").require("Song").build()
        self.register_intent(next_song_intent, self.handle_next_song)

        next_station_intent = IntentBuilder("PandoraNextStationIntent"). \
            require("Next").require("Station").build()
        self.register_intent(next_station_intent, self.handle_next_station)

        pause_song_intent = IntentBuilder("PandoraPauseIntent"). \
            require("Pause").require("Pandora").build()
        self.register_intent(pause_song_intent, self.handle_pause)

        resume_song_intent = IntentBuilder("PandoraResumeIntent"). \
            require("Resume").require("Pandora").build()
        self.register_intent(resume_song_intent, self.handle_resume_song)

        list_stations_intent = IntentBuilder("PandoraListStationIntent"). \
            optionally("Pandora").require("Query").require("Station").build()
        self.register_intent(list_stations_intent, self.handle_list)

        play_stations_intent = IntentBuilder("PandoraChangeStationIntent"). \
            require("Change").require("Station").build()
        self.register_intent(play_stations_intent, self.play_station)

    def on_websettings_changed(self):
        if not self._is_setup:
            email = self.settings.get("email", "")
            password = self.settings.get("password", "")
            try:
                if email and password:
                    self._is_setup = True
                    self._register_all_intents()
                    self._configure_pianobar()
            except Exception as e:
                LOG.error(e)

    def _configure_pianobar(self):
        # Initialize the Pianobar configuration file
        if self._is_setup:
            if not exists(self.pianobar_path):
                makedirs(self.pianobar_path)

            config_path = join(self.pianobar_path, 'config')
            with open(config_path, 'w+') as f:

                # grabs the tls_key needed
                tls_key = subprocess.check_output(
                    "openssl s_client -connect tuner.pandora.com:443 \
                    < /dev/null 2> /dev/null | openssl x509 -noout \
                    -fingerprint | tr -d ':' | cut -d'=' -f2",
                    shell=True)

                config = 'audio_quality = medium\n' + \
                         'tls_fingerprint = {}\n' + \
                         'user = {}\n' + \
                         'password = {}\n' + \
                         'event_command = {}'

                f.write(config.format(tls_key,
                                      self.settings["email"],
                                      self.settings["password"],
                                      self._dir + '/event_command.py'))

            # Raspbian requires adjustments to audio output to use PulseAudio
            platform = self.config_core['enclosure'].get('platform')
            if platform == 'picroft' or platform == 'mycroft_mark_1':
                libao_path = expanduser('~/.libao')
                if not isfile(libao_path):
                    with open(libao_path, 'w') as f:
                        f.write('dev=0\ndefault_driver=pulse')
                    self.speak_dialog("configured.please.reboot")
                    wait_while_speaking()
                    self.emitter.emit(Message('system.reboot'))

    def _load_vocab_files(self):
        # Keep a list of all the vocabulary words for this skill.  Later
        # these words will be removed from utterances as part of the station
        # name.
        vocab_dir = join(dirname(__file__), 'vocab', self.lang)
        if path.exists(vocab_dir):
            for vocab_type in listdir(vocab_dir):
                if vocab_type.endswith(".voc"):
                    with open(join(vocab_dir, vocab_type), 'r') as voc_file:
                        for line in voc_file:
                            parts = line.strip().split("|")
                            vocab = parts[0]
                            self.vocabs.append(vocab)
        else:
            LOG.error('No vocab loaded, ' + vocab_dir + ' does not exist')

    def _poll_for_pianobar_update(self, message):
        # Checks once a second for feedback from Pianobar

        # 'info_ready' file is created by the event_command.py
        # script when Pianobar sends new track information.
        info_ready_path = join(self.pianobar_path, 'info_ready')
        if isfile(info_ready_path):
            self._load_current_info()
            try:
                remove(info_ready_path)
            except Exception as e:
                LOG.error(e)

            # Update the "Now Playing song"
            LOG.info("State: "+str(self.piano_bar_state))
            if self.piano_bar_state == "playing":
                self.enclosure.mouth_text(self.settings["song_artist"] + ": " +
                                          self.settings["song_title"])

    def _init_pianobar(self):
        if self.settings.get('first_init') is False:
            return

        # Run this exactly one time to prepare pianobar for usage
        # by Mycroft.
        try:
            LOG.info("INIT PIANOBAR")
            subprocess.call("pkill pianobar", shell=True)
            self.process = subprocess.Popen(["pianobar"],
                                            stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE)
            time.sleep(3)
            self.process.stdin.write("0\n")
            self.process.stdin.write("S")
            time.sleep(0.5)
            self.process.kill()
            self.settings['first_init'] = False
        except:
            self.speak_dialog('wrong.credentials')

        self.process = None

    def _load_current_info(self):
        # Load the 'info' file created by Pianobar when changing tracks
        info_path = join(self.pianobar_path, 'info')

        # this is a hack to remove the info_path
        # if it's a directory. An earlier version
        # of code may sometimes create info_path as
        # a directory instead of a file path
        # date: 02-18
        if isdir(info_path):
            shutil.rmtree(info_path)

        if not exists(info_path):
            with open(info_path, 'w+'):
                pass

        with open(info_path, 'r') as f:
            info = json.load(f)

        # Save the song info for later display
        self.settings["song_artist"] = info["artist"]
        self.settings["song_title"] = info["title"]
        self.settings["song_album"] = info["album"]

        self.settings["station_name"] = info["stationName"]
        if self.debug_mode:
            LOG.info("Station name: " + str(self.settings['station_name']))
        self.settings["station_count"] = int(info["stationCount"])
        self.settings["stations"] = []
        for index in range(self.settings["station_count"]):
            station = "station" + str(index)
            self.settings["stations"].append(
                (info[station].replace("Radio", ""), index))
        if self.debug_mode:
            LOG.info("Stations: "+str(self.settings["stations"]))
        self.settings.store()

    def _start_pianobar(self):
        try:
            LOG.info("Starting Pianobar process")
            subprocess.call("pkill pianobar", shell=True)
            time.sleep(1)

            # start pandora
            if self.debug_mode:
                self.process = \
                    subprocess.Popen(["pianobar"], stdin=subprocess.PIPE)
            else:
                self.process = subprocess.Popen(["pianobar"],
                                                stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE)
            self.current_station = "0"
            self.process.stdin.write("0\n")
            self.handle_pause()
            time.sleep(2)
            self._load_current_info()
            LOG.info("Pianobar process initialized")
        except:
            self.speak_dialog('wrong.credentials')
            self.process = None

    def _extract_station(self, utterance):
        """
            parse the utterance for station names
            and return station with highest probability
        """
        try:
            # TODO: Internationalize

            common_words = [" to ", " on ", " pandora", " play"]
            for vocab in self.vocabs:
                utterance = utterance.replace(vocab, "")

            # strip out other non important words
            for words in common_words:
                utterance = utterance.replace(words, "")

            utterance.lstrip()
            stations = [station[0] for station in self.settings["stations"]]
            probabilities = fuzz_process.extractOne(
                utterance, stations, scorer=fuzz.ratio)
            if self.debug_mode:
                LOG.info("Probabilities: " + str(probabilities))
            if probabilities[1] > 70:
                station = probabilities[0]
                return station
            else:
                return None
        except Exception as e:
            LOG.info(e)
            return None

    def _play_station(self, station, dialog=None):
        LOG.info("Starting: "+str(station))
        if dialog:
            self.speak_dialog(dialog, {"station": station})
        else:
            self.speak_dialog("playing.station", {"station": station})
        self._start_pianobar()
        self.enclosure.mouth_think()

        for channel in self.settings.get("stations"):
            if station == channel[0]:
                self.process.stdin.write("s")
                self.current_station = str(channel[1])
                station_number = str(channel[1]) + "\n"
                self.process.stdin.write(station_number)
                self.piano_bar_state = "playing"
                self.settings["last_played"] = channel

    @intent_handler(IntentBuilder("").require("Play").require("Pandora"))
    def play_pandora(self, message=None):
        if self._is_setup:
            # Examine the whole utterance to see if the user requested a
            # station by name
            station = self._extract_station(message.data["utterance"])
            if self.debug_mode:
                LOG.info("Station request:" + str(station))

            dialog = None
            if not station:
                last_played = self.settings.get("last_played")
                if last_played:
                    station = last_played[0]
                    dialog = "resuming.last.station"
                else:
                    # default to the first station in the list
                    if self.settings.get("stations"):
                        station = self.settings["stations"][0][0]

            # Play specified station
            self._play_station(station, dialog)
        else:
            # Lead user to setup for Pandora
            self.speak_dialog("please.register.pandora")

    def handle_next_song(self, message=None):
        if self.process:
            self.enclosure.mouth_think()
            self.process.stdin.write("n")
            self.piano_bar_state = "playing"

    def handle_next_station(self, message=None):
        if self.process and self.settings.get("stations"):
            new_station = int(self.current_station) + 1
            if new_station >= int(self.settings.get("station_count", 0)):
                new_station = 0
            new_station = self.settings["stations"][new_station][0]
            self._play_station(new_station)

    def handle_pause(self, message=None):
        if self.process:
            self.process.stdin.write("S")
            self.piano_bar_state = "paused"

    def handle_resume_song(self, message=None):
        if self.process:
            self.process.stdin.write("P")
            self.piano_bar_state = "playing"

    def play_station(self, message=None):
        if self._is_setup:
            # Examine the whole utterance to see if the user requested a
            # station by name
            station = self._extract_station(message.data["utterance"])

            if station is not None:
                self._play_station(station)
            else:
                self.speak_dialog("no.matching.station")
        else:
            # Lead user to setup for Pandora
            self.speak_dialog("please.register.pandora")

    def handle_list(self, message=None):
        is_playing = self.piano_bar_state == "playing"
        if is_playing:
            self.handle_pause()

        # build the list of stations
        l = []
        for station in self.settings.get("stations"):
            l.append(station[0])  # [0] = name
        if len(l) == 0:
            self.speak_dialog("no.stations")
            return

        # read the list
        if len(l) > 1:
            list = ', '.join(l[:-1]) + " " + \
                   self.translate("and") + " " + l[-1]
        else:
            list = str(l)
        self.speak_dialog("subscribed.to.stations", {"stations": list})

        if is_playing:
            wait_while_speaking()
            self.handle_resume_song()

    def stop(self):
        if not self.piano_bar_state == "paused":
            self.handle_pause()
            return True

    @intent_handler(IntentBuilder("").require("Pandora").
                    require("Debug").require("On"))
    def debug_on_intent(self, message=None):
        if not self.debug_mode:
            self.debug_mode = True
            self.speak_dialog("entering.debug.mode")

    @intent_handler(IntentBuilder("").require("Pandora").
                    require("Debug").require("Off"))
    def debug_off_intent(self, message=None):
        if self.debug_mode:
            self.debug_mode = False
            self.speak_dialog("leaving.debug.mode.dialog")

    def shutdown(self):
        # Clean up before shutting down the skill
        subprocess.call("pkill pianobar", shell=True)
        if self.piano_bar_state == "playing":
            self.enclosure.mouth_reset()

        super(PianobarSkill, self).shutdown()


def create_skill():
    return PianobarSkill()
