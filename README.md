# GameOfDrones
My diplom work: simulator of drone's flying using AirSim, AirSim Neurlpc API, Tensorflow Object Detection API models

https://github.com/microsoft/AirSim-NeurIPS2019-Drone-Racing - базовый репозиторий, в readme есть инструкции по установке симулятора и некоторые команды API

Под Windows:
Симулятор запускается из консоли командой

  D:\AirSim\AirSim>AirSimExe.exe -windowed
  
Управление дроном запускается из virtualenv этого проекта командой

  (AirSim_python3.7) C:\Users\User\PycharmProjects\AirSim_python3.7\baselines>python baseline_racer.py --enable_viz_image_cv2 --planning_baseline_type all_ga
tes_at_once --planning_and_control_api my --level_name Soccer_Field_Easy --race_tier 1

При необычном завершении работы, когда на следующий запуск скрипт управления не подключается к симулятору, надо почистить порт 41451
  D:\AirSim\AirSim>netstat -ano | findstr :41451
  D:\AirSim\AirSim>taskkill /PID <PID из результата предыдущей команды> /F

В файле baseline_racer.py есть функция fly_itself - именно она отвечает за управление дроном (флаг --planning_and_control_api my при запуске).

Для сбора данных полезно запускать с флагом --planning_and_control_api moveOnSpline

Флаг --level_name - название трассы и сложность. В базовом репозитории подробнее
