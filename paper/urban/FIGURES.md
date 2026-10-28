# Рисунки окончательной редакции — входы, а не ожидаемые картинки

Ни один слот ниже не считается измеренным рисунком, пока нет реальных данных и
сохранённой команды построения. Использовать стандартный Matplotlib из существующего
document image после освобождения runtime lock. Новых обязательных пакетов нет.
Рекомендуемый итог: один прежний paired plot + четыре новых рисунка; пагинацию
10–12 страниц проверить по готовому PDF, без заполнения объёма выдуманными результатами.

1. **Первая заранее запланированная geometry-пара.** Выбрать первый block из
   frozen `cases` и его fixed/failover, независимо от успеха. Две actual XY-схемы
   с едиными корпусами и стрелками этапов; рядом/снизу actual Z и LOS/NLOS исходного
   CP↔UAV1. Входы: per-run `urban_trajectory.csv`, `urban_geometry_transitions.csv`,
   `urban_clocks.csv` и сохранённый scenario JSON. Не соединять stale/source gaps.
   Подпись поясняет70м подготовку,60м горизонтальный участок и12м фасадный этап;
   follower преимущественно удерживают точки при критическом событии. Плановые
   waypoint могут быть пунктирными; их нельзя показывать как actual trajectory.

2. **Первичные эффекты и все пары.** По `pairs.csv` показать fixed→failover для
   timely/offered и verified waypoint fraction, отдельно geometry/LOS-only.
   Полные пары/блоки — независимые точки, CI только из `urban_study.json`.
   Failed с полным valid окном отмечать крестиком; отсутствующий исход обозначать
   в счётчике missing, не ставить точку в0. Interaction берётся из четырёх случаев
   одного seed, а не из двух разных наборов доступных пар. Не выбирать удачный seed.

3. **Сценарный таймлайн с физикой.** Для того же frozen block: geometry-entry,
   фактический direct-probe timeout, selection, valid_from/activation, первый
   post-event CID→receive→UART и настоящее движение/достижение. Источники —
   native geometry/events, coordination JSONL, `urban_target_CID_stages.csv`,
   `urban_new_goal_after_NLOS.csv`, действительные позы. Все события на одной
   зарегистрированной host-monotonic шкале; ns-3 PCAP time не прибавляется напрямую
   к Gazebo source time. Показывать фактическое начало/конец, не суммировать максимумы
   разных UAV. Создание выбранной успешной цели и первый successful refresh раздельны.

4. **Границы и стоимость.** Sensitivity2,7/3,0/3,3 показывается по независимым
   парам; auxiliary n3/boundary n1 описательные, без заимствования main CI.
   Рядом RTF/lag по настоящим `urban_RTF.csv`/native stats иCPU/RSS из
   `urban_resources.csv`; GPU только при реально записанном измерении. Оси
   не заменяют пропуски нулями и не изображают исключительно успешные запуски.

Межуровневые счётчики offered→created CID→validated app receipt→UART→unique
waypoint лучше оставить отдельной таблицей builder: это разные сущности и
знаменатели. Столбец app receipt не называется PHY reception/ACK,27 waypoint не
называются27 независимыми полётами. All-followers native PHY proof относится к
отдельной реально разобранной пилотной паре, а не к покадровому аудиту всех56.

Рендерить PNG шириной не менее1642px для13,9см при300effective dpi, сохранять
SVG/PDF и фактические данные/команду построения. В manifest включать только готовые
файлы с RU/EN-подписями и `measured:true`. Это поле не заменяет независимое ревью.
