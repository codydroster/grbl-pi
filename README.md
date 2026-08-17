# grbl-pi

Two-carriage automated bin storage. The Pi is high level only; each carpark
Teensy owns its own sensor-driven sequencing; the car Teensy is a dumb
motor/sensor board.

## Starting the web server (normal operation)

```
./run.sh
```

(wraps `.venv/bin/python server.py` from the right folder - plain
`python3 server.py` uses the wrong python and will not work)

Then open `http://<pi>:8000` from any device on the network (iPad, phone,
laptop - any number at once). `/debug` is the remote debug shell.

One-time setup on the Pi (--system-site-packages matters: the venv must see
the system gpiozero, or every carriage fails to open):

```
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

then start with `.venv/bin/python server.py`.

## Debug shell (terminal)

```
python3 main.py --debug
```

Same commands as the web /debug page. The shell and the server CANNOT run at
the same time - they both need the serial ports.

## Updating the web frontend

The React source lives in `frontend/`; `web/` is the build the server serves:

```
cd frontend
npm run build
rm -rf ../web && cp -r build ../web
```

Then copy `web/` to the Pi along with the rest of this folder (the Pi does
not need `frontend/` or node at all).

For hot reload while editing (UI changes appear on save, with the real Pi
hardware behind them):

```
cd frontend
REACT_APP_API=http://<pi>:8000 REACT_APP_WS_URL=ws://<pi>:8000/ws npm start
```

## Files

- `server.py` - web server; owns the serial ports; WS + REST + static
- `main.py` - carriage config, debug shell, collision clearing
- `grbl.py` / `carpark.py` - the GRBL and carpark-Teensy links
- `positions.py` / `positions.json` - taught slot positions (BACK THIS UP -
  nothing can regenerate it)
- `slots.py` / `slots.json` - barcode -> "col,row,depth" assignments
- `data/` - bin inventory for the web app
- `frontend/` - React source; `web/` - its build output (what the Pi serves)
- `carpark1/`, `car1/` - Teensy firmware
