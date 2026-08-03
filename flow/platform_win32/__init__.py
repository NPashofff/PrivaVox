"""Windows имплементации на платформения слой — фаза W2 (docs/windows-port-plan.md).

Модули (истински от W2 нататък, вече не стъбове):
  stt_faster_whisper.py — SttEngine върху faster-whisper/CTranslate2 (CUDA
                          float16 / CPU int8; CPU пътят върви и на macOS —
                          така се тества без Windows машина)
  insert_win.py         — клипборд през ctypes (user32/kernel32) + Ctrl+V
                          през pynput/SendInput; без TCC еквивалент
  singleinstance_win.py — msvcrt.locking примитив за flow/singleinstance.py
  hud_win.py            — Tkinter пил (собствена Tk нишка + опашка)
  shell.py              — pystray tray приложението (аналогът на flow/app.py)

Самият пакет (този __init__) се import-ва безопасно на всички платформи;
модулите с Windows-only зависимости в горния ред (msvcrt) се import-ват само
зад sys.platform диспеча — вж. flow/platform_impl.py.
"""
