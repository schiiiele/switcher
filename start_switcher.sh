#!/bin/bash
# 참고용 수동 실행 스크립트. 평소엔 launchd(com.switcher.local)가 자동 관리.
nohup python3.11 ~/dev/switcher/switcher_server.py > ~/dev/switcher/switcher.log 2>&1 &
echo "✅ 서버 시작됨! PID: $!"
