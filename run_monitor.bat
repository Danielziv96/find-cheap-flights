@echo off
cd /d "c:\projects\find_cheap_flights"
python flight_monitor.py >> monitor_log.txt 2>&1
