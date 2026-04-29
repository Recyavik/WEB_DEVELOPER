#!/bin/sh
printf 'nameserver 8.8.8.8\nnameserver 8.8.4.4\n' > /etc/resolv.conf
exec uvicorn main:app --host 0.0.0.0 --port 5000
