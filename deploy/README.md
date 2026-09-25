# 아이맥(상시 가동 머신) 배포 가이드

부팅 시 자동 시작 + 크래시 시 자동 재시작(launchd). 노트북 세션과 달리 꺼지지 않습니다.

## ⚠️ 반드시 지킬 것: 엔진은 한 곳에서만

KIS는 **appkey당 WebSocket 동시접속 1개**입니다. 아이맥에서 켜기 전에
다른 컴퓨터의 엔진을 반드시 끄세요 (`./stop_paper.sh`). 두 곳에서 켜면 서로 접속을 뺏으며 오작동합니다.

## 1. 프로젝트 옮기기

```bash
# 아이맥에서
git clone <저장소> ~/workspace/short_trading_bot   # 또는 폴더 통째로 복사
cd ~/workspace/short_trading_bot
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]" finance-datareader
.venv/bin/alembic upgrade head
```

git에 없는 파일 3개는 **직접 복사**해야 합니다 (USB/AirDrop — 메일 금지):
- `.env.local` (KIS 키·비밀번호)
- `watchlist.json` (포트폴리오 설정)
- `data/minutes/` (선택 — 분봉 캐시, 없으면 백필이 다시 받음)

확인: `.venv/bin/trader preflight` → `ready=True`

## 2. 잠자기 방지 (필수)

시스템 설정 → 에너지 절약 → **"디스플레이가 꺼져 있을 때 자동으로 잠자기 방지" 켜기**
(디스플레이는 꺼져도 됨 — 시스템 잠자기만 막으면 됩니다)

터미널로 하려면: `sudo pmset -a sleep 0`

## 3-A. 일상 운용: tmux (권장 — 실행 화면을 직접 보며 운용)

```bash
brew install tmux          # 최초 1회
./run_paper.sh             # tmux 세션 'stb'에 엔진+API 가동
tmux attach -t stb         # 실시간 화면 보기 (창 전환 Ctrl-b n, 분리 Ctrl-b d)
./stop_paper.sh            # 중지
```
tmux 세션은 터미널을 닫아도 유지됩니다(재부팅 전까지). 재부팅 후 자동 시작까지
원하면 **3-A′(tmux 자동 시작)** 를 등록하세요. ⚠️ 3-B(engine/api 직접 실행)를
tmux 운용과 같이 쓰면 엔진이 두 개 떠서 KIS WS 접속을 서로 뺏는다 — 둘 중 하나만.

## 3-A′. (권장 조합) tmux + 로그인 자동 시작 + 5분 워치독

재부팅되면 tmux 세션이 사라지고(2026-07-31 실제 공백), 엔진 창만 크래시로 죽을 수도
있다(2026-08-03 나흘 방치). LaunchAgent가 로그인 시 + 5분마다 `run_paper.sh --watchdog`를
실행해 **죽은 창만** 되살린다. 의도된 중지(`./stop_paper.sh`·대시보드 긴급중지)는
`data/engine_stopped.marker`가 남아 워치독이 존중한다 — 재개는 `./run_paper.sh` 수동 실행만.

```bash
cd ~/workspace/short_trading_bot
sed "s|__PROJECT__|$(pwd)|g" deploy/com.shorttradingbot.tmux.plist \
  > ~/Library/LaunchAgents/com.shorttradingbot.tmux.plist
launchctl load ~/Library/LaunchAgents/com.shorttradingbot.tmux.plist
```

- FileVault 켠 맥은 재부팅 후 **로그인해야** 시작된다 (자동 로그인 설정 시 무인 부팅도 가능)
- 크래시 자동 재시작은 안 함 (그게 필요하면 아래 3-B — 단 **3-B와 동시 등록 금지**)
- 해제: `launchctl unload ~/Library/LaunchAgents/com.shorttradingbot.tmux.plist`

## 3-B. (선택) launchd 등록 — 부팅 자동 시작 + 크래시 자동 재시작

```bash
cd ~/workspace/short_trading_bot
mkdir -p logs
# __PROJECT__ 를 실제 경로로 치환해 설치
sed "s|__PROJECT__|$HOME/workspace/short_trading_bot|g" deploy/com.shorttradingbot.engine.plist \
  > ~/Library/LaunchAgents/com.shorttradingbot.engine.plist
sed "s|__PROJECT__|$HOME/workspace/short_trading_bot|g" deploy/com.shorttradingbot.api.plist \
  > ~/Library/LaunchAgents/com.shorttradingbot.api.plist

launchctl load ~/Library/LaunchAgents/com.shorttradingbot.engine.plist
launchctl load ~/Library/LaunchAgents/com.shorttradingbot.api.plist
```

동작 방식:
- **engine**: 부팅 시 시작, 크래시면 10초 후 자동 재시작. 단 **긴급중지(킬스위치)로 정상 종료하면
  재시작하지 않음** — 비상 정지가 멋대로 되살아나지 않게 설계.
- **api**: 항상 재시작 (대시보드는 늘 접속 가능해야 하므로)

관리 명령:
```bash
launchctl list | grep shorttradingbot          # 상태 확인
tail -f ~/workspace/short_trading_bot/logs/serve.log   # 로그
launchctl unload ~/Library/LaunchAgents/com.shorttradingbot.engine.plist  # 중지
launchctl load   ~/Library/LaunchAgents/com.shorttradingbot.engine.plist  # (재)시작
```

## 4. 다른 기기에서 대시보드 접속

같은 와이파이의 폰/노트북에서: `http://<아이맥IP>:8000` (아이맥IP는 시스템 설정→네트워크)
- 로그인은 `.env.local`의 `STB_API_USERNAME/PASSWORD`
- ⚠️ `.env.local` 에 `STB_API_JWT_SECRET`(`openssl rand -hex 32`)과 기본값이 아닌 `STB_API_PASSWORD`
  가 없으면 API 가 **기동을 거부**한다 (공개된 기본값이면 같은 와이파이의 누구나 전량청산을 누를 수
  있어서). 아이맥에서만 볼 거면 `trader api --host 127.0.0.1`.
- 프론트(PWA)까지 쓰려면 아이맥에서 `cd frontend && npm install && npm run dev -- --host`
  → `http://<아이맥IP>:5173` (다른 오리진이므로 `STB_API_CORS_ORIGINS=["http://<아이맥IP>:5173"]` 필요)
- **집 밖에서** 접속하려면 포트를 그냥 열지 말고 Tailscale(무료 VPN) 권장 — 설치만 하면
  외부에서도 안전하게 같은 주소로 접속됩니다.

## 5. 운영 습관

- Discord 알림이 오니 평소엔 볼 필요 없고, 주 1회 `logs/serve.log`와 대시보드 손익만 점검
- macOS 업데이트 자동 재부팅은 장중을 피하도록 설정 (시스템 설정 → 일반 → 소프트웨어 업데이트)
- KIS API 키 만료(발급일로부터 1년) 알림을 캘린더에
