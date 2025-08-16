[app]
title = HebelWatch Lite
package.name = hebelwatchlite
package.domain = biz.jurina
source.dir = .
source.include_exts = py,kv,png,jpg,jpeg,wav,txt,csv

requirements = python3,openssl,urllib3,certifi,chardet,idna,requests, \
    beautifulsoup4,lxml, \
    pytz,yfinance, \
    kivy

orientation = sensor
fullscreen = 0
android.permissions = INTERNET, WAKE_LOCK
android.api = 33
android.minapi = 21
android.ndk = 25b
android.logcat_filters = python:D,ActivityManager:I

[buildozer]
log_level = 2
warn_on_root = 1

