# Здания, горизонтальный облёт и граница доказанного

Дата 28.10.2026, чтение snapshot `bb026a052a2474df29bee3603cc98af27592e207`; native ns-3.48 `d2add90b452d600cfb4859baed8e9ea633519447`. Этот отчёт — code/asset audit и **план**, не результат нового NLOS-полёта. PDF: `paper/build/manuscript_anonymous.pdf`, разделы 3–6; текст извлечён `pdftotext -layout`. Старые raw не изменялись, новые симуляции автором этого отчёта не запускались.

## Что реально поддерживает snapshot

| Наблюдение | Первичный источник | Следствие |
|---|---|---|
| Relay-профиль жёстко требует `PROPAGATION_PROFILE == friis` | `network/ns3/run_native_radio_five_uav.sh:232–240` | `BAS_NATIVE_PROPAGATION_PROFILE=sionna` при включённом coordinator не является рабочей командой городского эксперимента. Runner отвергает комбинацию. |
| Friis возвращает штатный `FriisSpectrumPropagationLossModel` без чтения препятствий | `network/ns3/scratch/native-spectrum-sources.h:35–62` | Городская картинка и LOS/NLOS названия точек не создают радиозатенение. |
| Матрица добавляет объявленные 200 dB по паре/фазе | `upstream-sionna-tap-spike.cc:1062`, `:1680–1686` | Это инъекция отказа, не эффект здания; её нельзя переименовать в NLOS. |
| C++ имеет настоящую Sionna RT цепочку с scene/frequency/solver | `upstream-sionna-tap-spike.cc:1687–1725` | Поддержка существует ниже relay runner, но её наличие не доказывает работу live-полёта с handover. |
| Сцена Gazebo и RT выровнены identity в CAVISE frame | `.external/cavise_maps/Town01/gazebo/alignment.json` | Меши потенциально пригодны для общей геометрии; у alignment recorded sampled_mesh_count=140/max_vertex_delta=0. Нужна проверка конкретных LOS-отрезков и радиоматериалов. |
| Gazebo использует building collision boxes, surfaces и visual meshes | `scripts/product/prepare_town01_gazebo.py:259–310` | Коллизии физики и радиопрепятствия — разные подсистемы; эти collision boxes не импортируются в native BuildingList автоматически. |
| Основной relay driver сохраняет начальные широту/долготу и XY | `scripts/product/relay_experiment.py:521–526`, `:591–597` | Основные30 и дополнительные18 — вертикальные высотные задания; нельзя описывать их как облёт квартала. |
| Completion хранит max_vertical_displacement | `scripts/product/relay_experiment.py:179–205`; goal recovery в analyzer | Горизонтальную цель нельзя пропускать через прежний критерий «новая высота/вертикальный манёвр» без отдельного профиля и критерия. |
| Старый product YAML содержит горизонтальный маршрут только UAV1 | `network/config/scenario_5uav_town01_native_product.yaml:101–123` | Уже есть путь вдоль улицы/за квартал для отдельного драйвера; он не превращает relay mission в маршруты всех пяти. |

На интеграции дополнительно реально вызван runner с failover+Sionna при остальных допустимых realtime/5UAV/ad-hoc/control настройках. Он завершился кодом2 с сообщением `Coordination requires ... explicit Friis/ad-hoc` до старта ns-3/полёта. Точный argv/env/output — `urban_relay_guard.json`. Это проверка запрета конфигурации, не запуск RT solver и не измерение его производительности.

## Можно ли подключить лёгкое затенение без собственных радиоформул

В установленном upstream есть `src/buildings/model/buildings-channel-condition-model.cc:44–77,156–176`: outdoor LOS определяется пересечением отрезка с `Building::IsIntersect`. Это настоящая пространственная классификация, но **не модель ослабления**. Нужны Building объёмы, `MobilityBuildingInfo` и обновление indoor/outdoor состояния при движении. В измеренной native-цепочке этого нет.

`OhBuildingsPropagationLossModel::GetLoss` для outdoor/outdoor вызывает Okumura–Hata; простая замена на эту модель не доказывает экранирование промежуточным зданием. Аналогично название HybridBuildings не означает ray-tracing. Связка BuildingsChannelConditionModel + upstream 3GPP pathloss может дать явный LOS/NLOS approximation, однако выбор модели должен соответствовать частоте и высотам. В `src/propagation/model/three-gpp-propagation-loss-model.cc` есть `EnforceParameterRanges`, специальные BS/UT высоты и область применимости. Для НПУ2м и A2A12–20м она здесь не проверена. Не следует отключать предупреждения применимости и объявлять результат физически корректным.

Минимальная геометрически содержательная ветка — повторно использовать уже имеющийся Sionna RT provider, общий mesh/материалы и реальные poses; перед full-stack проверить LOS и обходной путь на статичных позициях. Но relay guard, горизонты latency/cache и цель/критерий движения требуют изменений. RTX/GPU и импорт пакетов не доказывают OptiX solver или соблюдение live deadline. `hybrid` тоже запрещён relay guard и не является автоматическим безопасным fallback: код использует Friis лишь в отдельно определённой far-field области.

## Предложенный прикладной сценарий — ещё не реализован и не измерен

`scenario/industrial_block_plan.json` и `.svg`: осмотр двух корпусов, пять аппаратов идут разными горизонтальными маршрутами. Это **идеализированная новая площадка**, не выдуманные координаты Town01. Одни и те же объёмы должны породить Gazebo visual/collision и RT-геометрию либо upstream Building объекты. Planned release times не равны времени достижения точки или фактического NLOS.

НПУ `[0,0,2]`; корпусA x40…70/y20…65/z0…30, B x95…125/y20…65/z0…25. Первоначальный relay UAV1 идёт по южной улице, затем по проходу x85 к северной стороне. Прямая НПУ→UAV1 у северной точки `[85,80,12]` пересекает A, в то время как путь НПУ→западный UAV3 `[15,90,16]` и UAV3→UAV1 идёт вне объёмов. UAV5 обходит B с востока, создавая второй NLOS-участок. Эти ожидания геометрические; успех связи/переназначение нельзя заранее назначить результатом.

При потере lease текущий runtime отправляет BRAKE. Поэтому fixed может остановиться за углом и **не выйти из NLOS**. Нельзя обещать timed LOS return либо двигать Gazebo poses, чтобы восстановление случилось. Неисполненная цель/отсутствующий выход сохраняются как цензурированные/неизмеренные исходы.

## Минимальные изменения и критерий измеренного результата

1. Отдельный именованный профиль без изменения `8e16ec2/e6453c8`; разрешить в runner только явно поддержанную obstacle-aware цепочку, без скрытого Friis fallback. Протокол lease/выбор кандидатов сначала сохранить.
2. Тонкий сценарный адаптер к штатным MAVLink Guided XYZ/global setpoints, проверка frame/home/yaw, маршруты всех пяти. Existing latitude-offset helper не подтверждён для нового frame; трансформацию проверить по настоящим перемещениям. Не писать физику/автопилот.
3. Отделить горизонтальный completion от старого max-vertical критерия: world XY displacement, достижение нового XYZ, dwell, speed, valid first CID/goal/epoch. Сохранить UART-receive и write-end раздельно.
4. Подтвердить два неподвижных случая LOS/NLOS upstream provider на одних координатах; проверить обходной relay path и отсутствие двойного pathloss. Затем один physical pilot и paired fixed/failover с теми же маршрутами/seed, без искусственной матрицы; инъекции2/5/10/30с выделить другой фактор.
5. Логировать actual positions и model/host clocks, LOS state каждого CP↔UAV/UAV↔UAV линка, blocker/scene identity, path gain/RT cache age, receive PSD/SNR, packet failures, selection/epoch/grants, CID admission и physical goal. Не выводить LOS из одного потерянного пакета или успешной доставки.
6. Entry/exit NLOS вычислять по реальным poses/модели, не времени задания. Для «доли после выхода» заранее задать denominator: все offered после подтверждённого выхода в фиксированном окне, UART успех отдельно; если выхода нет, endpoint N/A/censored, не 0 и не100%.

Объём можно ограничить одним кварталом и одной парой, но изменения пересекают runner, radio chain, mission adapter и метрики. Это не честный env-only быстрый повтор. В текущей статье следует оставить urban shadowing/horizontal topology как limitation/future work до успешных native model probes и physical pair. Новый сценарий нельзя приписать прежним30/18 или объявить измеренным по этой схеме.

## Первичные источники и воспроизведение проверки

Локальный upstream ns-3.48 — `.external/ns-3-sionna-native/src/buildings/model/{building,buildings-channel-condition-model,oh-buildings-propagation-loss-model}.cc`, `src/propagation/model/three-gpp-propagation-loss-model.cc`; точная версия указана выше. [Официальная ns-3 документация Buildings](https://www.nsnam.org/docs/models/html/buildings-design.html) при web-проверке28.10 не загрузилась; выводы о коде сделаны по pinned checkout. [Официальный Sionna RT API](https://nvlabs.github.io/sionna/rt/api/rt.html) описывает scenes/materials/path solver, но текущая online версия2.2 отличается от pinned BAS1.2.0; API-совместимость не предполагается.

```bash
pdftotext -layout paper/build/manuscript_anonymous.pdf /tmp/bas-obstacle-paper.txt
rg -n 'PROPAGATION_PROFILE.*friis|Coordination requires' network/ns3/run_native_radio_five_uav.sh
rg -n 'SionnaRtSpectrum|AddPropagationLossModel|AddPhasedArraySpectrum' network/ns3/scratch/upstream-sionna-tap-spike.cc
rg -n 'anchor\[|initial\[i\].position|max_vertical_displacement' scripts/product/relay_experiment.py
# From integration, read-only: git -C .external/ns-3-sionna-native rev-parse HEAD
```
