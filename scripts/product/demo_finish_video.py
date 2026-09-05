#!/usr/bin/env python3
"""Join chapters, produce index and timecode coverage, and check real encoded artifacts."""
import argparse,csv,json,re,shutil,subprocess
from pathlib import Path
from PIL import Image,ImageStat
from demo_video_data import readjson,jsonlines
from demo_video import tc
ROOT=Path(__file__).resolve().parents[2]


def probe(path):
 return json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-show_streams','-show_chapters','-of','json',str(path)]))

def seconds(value):
 h,m,s=value.replace(',','.').split(':');return int(h)*3600+int(m)*60+float(s)


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args();out=a.output.resolve()
 configs=[json.loads(p.read_text()) for p in sorted((ROOT/'network/config/demo').glob('*.json'))]
 infos=[];offset=0;metadata=[';FFMETADATA1'];subtitles=[];paths=[]
 for config in configs:
  path=out/'videos'/(config['id']+'.mp4');info=probe(path);video=next(s for s in info['streams'] if s['codec_type']=='video');duration=float(info['format']['duration'])
  assert video['codec_name']=='h264' and (video['width'],video['height'])==(1920,1080)
  assert video['r_frame_rate'] in ('25/1','30/1')
  row=dict(scenario=config['id'],path=str(path),duration_s=duration,size_bytes=path.stat().st_size,format='H.264 MP4, 1920x1080, 25 FPS',chapter_start=offset)
  infos.append(row);paths.append(path)
  metadata+=['[CHAPTER]','TIMEBASE=1/1000',f'START={round(offset*1000)}',f'END={round((offset+duration)*1000)}','title='+config['title']]
  blocks=(out/'subtitles'/(config['id']+'.srt')).read_text().strip().split('\n\n')
  for block in blocks:
   lines=block.splitlines()
   if len(lines)<3:continue
   lo,hi=lines[1].split(' --> ')
   subtitles.append(f'{len(subtitles)+1}\n{tc(seconds(lo)+offset).replace(".",",")} --> {tc(seconds(hi)+offset).replace(".",",")}\n'+'\n'.join(lines[2:]))
  offset+=duration
 listing=out/'reports/combined-concat.txt';listing.write_text(''.join("file '"+str(path).replace("'","'\\''")+"'\n" for path in paths))
 metapath=out/'reports/chapters.ffmeta';metapath.write_text('\n'.join(metadata)+'\n')
 subtitle=out/'subtitles/BAS_v2_RC1_demo_ru.srt';subtitle.write_text('\n\n'.join(subtitles)+'\n')
 combined=out/'videos/BAS_v2_RC1_demo_ru.mp4'
 subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listing),'-i',str(metapath),'-i',str(subtitle),
  '-map','0:v:0','-map','2:0','-map_metadata','1','-c:v','copy','-c:s','mov_text','-metadata:s:s:0','language=rus','-movflags','+faststart',str(combined)],check=True)
 data=probe(combined);assert len(data['chapters'])==5
 infos.append(dict(scenario='BAS_v2_RC1_demo_ru',path=str(combined),duration_s=float(data['format']['duration']),size_bytes=combined.stat().st_size,format='H.264 MP4, 1920x1080, 25 FPS; Russian subtitles; 5 chapters',chapter_start=0))
 quality=[]
 for row in infos:
  path=Path(row['path']);log=out/'reports'/(path.stem+'-decode.log')
  with log.open('w') as stream:
   check=subprocess.run(['ffmpeg','-hide_banner','-v','error','-i',str(path),'-map','0:v:0','-f','null','-'],stdout=stream,stderr=stream)
  assert check.returncode==0,log
  samples=[]
  for name,t in [('start',.4),('middle',row['duration_s']/2),('end',max(.4,row['duration_s']-1))]:
   dest=out/'screenshots'/(path.stem+'-'+name+'.jpg')
   subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(t),'-i',str(path),'-frames:v','1',str(dest)],check=True)
   im=Image.open(dest).convert('RGB');stats=ImageStat.Stat(im)
   assert max(stats.stddev)>12,'Blank/flat sample: '+str(dest)
   scene_stats=ImageStat.Stat(im.crop((24,114,1304,634)))
   assert max(scene_stats.stddev)>2,'Blank/flat Gazebo scene: '+str(dest)
   samples.append(dict(time_s=t,file=str(dest),channel_stddev=stats.stddev,scene_channel_stddev=scene_stats.stddev))
  quality.append(dict(**row,decoded_without_errors=log.stat().st_size==0,samples=samples))
 (out/'reports/video_quality.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2)+'\n')
 (out/'reports/videos.json').write_text(json.dumps(infos,ensure_ascii=False,indent=2)+'\n')
 timeline=list(csv.DictReader((out/'edit_timeline.csv').open()))
 chapter_offsets={r['scenario']:r['chapter_start'] for r in infos}
 def pick(num,word):return next(r for r in timeline if r['scenario'].startswith(num) and word in r['title'])
 selections=[
 ('R1','Пять реальных SITL: взлёт, удержание, LAND и auto-disarm',pick('01','Посадка'),'metrics/scenario_summary.json','software demonstration'),
 ('R2','Десять SERIAL1/SERIAL2, P2P, 20 multicast roots, 50 one-shot',pick('03','UART'),'metrics/scenario_summary.json','software demonstration'),
 ('R3','Native ns-3 Wi-Fi + Sionna, двунаправленные результаты',pick('02','Открытая'),'metrics/received_energy.csv','software demonstration'),
 ('R4','Отдельные native waveform source cases и восстановление',pick('04','continuous'),'logs/native_radio_events.csv','software demonstration'),
 ('R5','Разделение полезной мощности, RSSI/SINR и packet outcomes',pick('02','Точка за'),'metrics/received_energy.csv','software demonstration'),
 ('R6','Serial/PTy, UDP/TCP с настоящим UART SITL; физический FC отсутствует',pick('05','Внешний serial'),'external_endpoint/metrics.json','software demonstration + hardware blocked'),
 ('R7','RTF/lag с записью; исходные RC1 radio-only дальности и масштаб',pick('05','Operating'),'../../metrics/recording_performance.json','measured envelope; not hard real-time'),
 ('R8','Town01, синтетические поле/холмы, mesh 10×10 км и башня',pick('01','Customer'),'../../reports/geometry_summary.json','software demonstration; synthetic additions'),
 ('R9','MAVProxy, выбор SYSID2, настоящий REQUEST_MESSAGE ACK',pick('01','НПУ MAVProxy'),'video/operator_io.jsonl','software demonstration'),
 ('R10','Действующие команды, локальная поставка и ограничения',pick('05','Поставка'),'../../INDEX.md','internal demo; asset redistribution unresolved')]
 coverage=[]
 for requirement,property_value,row,report,status in selections:
  local=float(row['final_video_start'])+1
  if requirement=='R1':local=float(row['final_video_end'])-1
  if requirement in ('R6','R9'):
   events=jsonlines(out/'raw'/row['source_run_id']/'video/operator_io.jsonl')
   ack=next((e for e in events if 'Got COMMAND_ACK: REQUEST_MESSAGE: ACCEPTED' in e.get('output','') and float(row['source_start_monotonic_s'])<=e['monotonic_ns']/1e9<=float(row['source_end_monotonic_s'])),None)
   if ack:local=float(row['final_video_start'])+(ack['monotonic_ns']/1e9-float(row['source_start_monotonic_s']))/float(row['speed'])
  if requirement=='R2':
   run=out/'raw'/row['source_run_id'];events=jsonlines(run/'logs/scenario_events.jsonl')
   event=next((e for e in events if e.get('detail')=='communications_complete'),None)
   if event:local=float(row['final_video_start'])+(event['monotonic_ns']/1e9-float(row['source_start_monotonic_s']))+.5
  if requirement=='R4':
   events=list(csv.DictReader((out/'raw'/row['source_run_id']/'logs/native_radio_events.csv').open()))
   event=next((e for e in events if e['event']=='jammer_on'),None)
   if event:local=float(row['final_video_start'])+(int(event['wall_monotonic_ns'])/1e9-float(row['source_start_monotonic_s']))+.2
  if requirement=='R5':
   events=jsonlines(out/'raw'/row['source_run_id']/'logs/scenario_events.jsonl')
   event=next((e for e in events if e.get('detail')=='obstructed_candidate'),None)
   if event:local=float(row['final_video_start'])+(event['monotonic_ns']/1e9-float(row['source_start_monotonic_s']))+.5
  coverage.append(dict(requirement=requirement,property_or_limit=property_value,scenario=row['scenario'],timecode=tc(local),combined_timecode=tc(local+chapter_offsets[row['scenario']]),confirming_report=str((out/'raw'/row['source_run_id']/report).resolve()),status=status))
 with (out/'requirements_video_matrix.csv').open('w') as stream:
  writer=csv.DictWriter(stream,fieldnames=list(coverage[0]));writer.writeheader();writer.writerows(coverage)
 performance=readjson(out/'metrics/recording_performance.json',[])
 lines=['# BAS v2 RC1 — внутренний демонстрационный пакет','',
  'Физический FC не подключён. R6: software demonstration + hardware blocked. Аппаратный HitL не подтверждён.','',
  '## Видео','', '| Файл | Длительность | Размер | Формат |','|---|---:|---:|---|']
 for row in infos:lines.append(f'| [videos/{Path(row["path"]).name}](videos/{Path(row["path"]).name}) | {tc(row["duration_s"])} | {row["size_bytes"]/1048576:.1f} MiB | {row["format"]} |')
 lines+=['','## Воспроизведение','', 'Из `/home/bas/bas_v2-rc1`: `make demo-record SCENARIO=01` (01…05), `make demo-record-all`, `make demo-video`, `make stop`. Для нового набора задайте `DEMO_OUTPUT=/absolute/new/path`.','',
  'Уже завершённые сценарии не перезаписываются. `demo-record-all` продолжает незавершённый набор; сборка не запускает SITL/Gazebo повторно.','',
  '## Время и происхождение','',
  '[edit_timeline.csv](edit_timeline.csv) связывает монтаж, run_id, исходный AVI и host monotonic. AVI хранит последовательность реально полученных кадров с индексной шкалой 25 FPS; фактическая частота измеряется отдельно. Для физического времени исходных кадров авторитетен `raw/<run_id>/video/frames.jsonl`, а не номинальный AVI FPS. В монтаже исходные кадры выбираются по host monotonic; искусственная интерполяция не используется.','',
  'Смена run_id и вырезанные ожидания явно обозначены. Метрики — синхронизированная композиция из журналов, не новый live dashboard. Исходные Gazebo AVI и окно MAVProxy сохранены непрерывно. Mодельное время Gazebo и ns-3 не приравнено wall time.','',
  'SRT находятся в [subtitles/](subtitles/). Общий MP4 содержит пять глав и дорожку русских субтитров. Озвучки и музыки нет.','',
  '## Покрытие','', '| R | Что показано / ограничение | Сценарий | Timecode | Общий фильм |','|---|---|---|---|---|']
 for row in coverage:lines.append(f'| {row["requirement"]} | {row["property_or_limit"]} | {row["scenario"][:2]} | {row["timecode"]} | {row["combined_timecode"]} |')
 lines+=['','[requirements_video_matrix.csv](requirements_video_matrix.csv) содержит ссылки на подтверждающие reports.','',
  '## Производительность во время записи','', '| Run | Камера: фактический FPS | RTF по часам Gazebo | ns-3 lag p95 / max после t=10 с, мс |','|---|---:|---:|---:|']
 for row in performance:
  cam=row['cameras']['uav_focus'];lag=row.get('ns3_lag_after_native_10s') or {}
  lines.append(f'| {row["run_id"]} | {cam["actual_fps"]:.2f} | {cam["gazebo_clock_rtf"]:.3f} | {lag.get("p95_ms", "нет данных")} / {lag.get("max_ms", "нет данных")} |')
 lines+=['','Cold lag сохранён отдельно в [metrics/recording_performance.json](metrics/recording_performance.json). Оба flight runs 01 и 02 не прошли штатный real-time gate: у 01 RTF около 0,776; у 02 RTF около 0,988, но ns-3 steady lag p95 около 71,92 мс. Успешный полёт не переименован в успешный performance check.','',
  '## Ограничения','',
  'Автономная миссия не доказывает доставку команд во время outage. Кэш 20 с / 10 м может задержать исчезновение пути и пропустить короткое восстановление. Камерный ракурс не классифицирует LOS. Energy SINR не заменяет decoder-weighted SINR. Нулевой знаменатель PDR остаётся «нет данных»; BLER Wi-Fi — not_applicable. SERIAL2 — MAVLink payload, не видео.','',
  'Карты 8×8 при z=2 м построены отдельным native pipeline для той же reference continuous конфигурации; это prediction, не измеренный PDR. Опубликованные RC1 benchmarks показаны отдельно с исходными run_id: radio-only 16 STA не означают 16 SITL; при 10 dBm дальние ссылки дали outage.','',
  'Демопакет предназначен для внутреннего просмотра. Передача CAVISE assets третьим лицам требует отдельного подтверждения. Сами assets и runtime image остаются в локальной поставке RC1.','',
  '## Проверка','', '[reports/video_quality.json](reports/video_quality.json): FFprobe, полное декодирование, кадры начала/середины/конца. Ключевые кадры монтажа — [screenshots/](screenshots/).','']
 (out/'INDEX.md').write_text('\n'.join(lines))
 print(json.dumps(infos,ensure_ascii=False,indent=2))
 return 0
if __name__=='__main__':raise SystemExit(main())
