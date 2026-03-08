#!/bin/bash
cd "$(dirname "$0")"
ls frontend.py server.py channel_feishu.py | entr -r python3 server.py
