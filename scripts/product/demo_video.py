#!/usr/bin/env python3
"""Compose current Gazebo footage and real operator captures with time-joined log overlays."""
import argparse
import bisect
import csv
import json
import math
import re
from pathlib import Path
import subprocess
import sys
import textwrap
from functools import lru_cache

import numpy as np
from PIL import Image,ImageDraw,ImageFont
from demo_video_data import Run,Series,readjson,number,details
from inject_native_radio_runtime_cameras import TOUR_CAMERAS

ROOT=Path(__file__).resolve().parents[2]
W,H,FPS=1920,1080,25
FONT='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
BOLD='/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
COLORS=['#fa6b6b','#79dfad','#d893ed','#f4cf70','#69bdfa']
CAMERAS={
 'overview':dict(pose=[50,55,140,0,1.5707,0],horizontal_fov=1.4),
 'obstacle':dict(pose=[80,110,70,0,1.5707,0],horizontal_fov=1),
 'uav_focus':dict(pose=[50,0,90,0,1.5707,1.5708],horizontal_fov=1.2),**TOUR_CAMERAS}

def fmt(v,digits=1):return 'нет данных' if v is None else f'{float(v):.{digits}f}'
def tc(t):
 t=max(0,t);return f'{int(t)//3600:02}:{int(t)//60%60:02}:{t%60:06.3f}'
@lru_cache(maxsize=32)
def font(size,bold=False):return ImageFont.truetype(BOLD if bold else FONT,size)
def text(draw,xy,value,size=23,color='#eaf0f7',bold=False):
 draw.text(xy,str(value),font=font(size,bold),fill=color)
def wrap(draw,xy,value,width=85,size=25,color='#eaf0f7'):
 lines=textwrap.wrap(value,width=width)
 for i,line in enumerate(lines):text(draw,(xy[0],xy[1]+i*(size+9)),line,size,color)
 return len(lines)*(size+9)

def project(p,camera):
 spec=CAMERAS[camera];x,y,z,r,pitch,yaw=spec['pose']
 cr,sr=math.cos(r),math.sin(r);cp,sp=math.cos(pitch),math.sin(pitch);cy,sy=math.cos(yaw),math.sin(yaw)
 rot=np.array([[cy,-sy,0],[sy,cy,0],[0,0,1]])@np.array([[cp,0,sp],[0,1,0],[-sp,0,cp]])@np.array([[1,0,0],[0,cr,-sr],[0,sr,cr]])
 f,l,u=rot.T@(np.asarray(p)-[x,y,z])
 if f<=.1:return None
 focal=1280/(2*math.tan(spec['horizontal_fov']/2));px=640-focal*l/f;py=360-focal*u/f
 return (24+px,114+py) if 0<=px<1280 and 0<=py<720 else None

class Decoder:
 def __init__(self,path,start=0,scale=None):
  probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','json',str(path)]))['streams'][0]
  self.width,self.height=probe['width'],probe['height']
  if scale:self.width,self.height=scale
  cmd=['ffmpeg','-hide_banner','-loglevel','error','-ss',str(start),'-i',str(path)]
  if scale:cmd+=['-vf',f'scale={scale[0]}:{scale[1]}']
  self.proc=subprocess.Popen(cmd+['-an','-threads','2','-f','rawvideo','-pix_fmt','rgb24','-'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
  self.index=-1;self.last=None
 def get(self,index):
  while self.index<index:
   count=self.width*self.height*3;buf=self.proc.stdout.read(count)
   if len(buf)!=count:break
   self.last=Image.frombytes('RGB',(self.width,self.height),buf);self.index+=1
  return self.last
 def close(self):
  self.proc.stdout.close();self.proc.terminate();self.proc.wait()


def clips(config,runs):
 num=config['id'][:2];result=[]
 def add(run,lo,hi,camera,title,caption,panel='metrics',speed=1):
  lo=max(lo,run.start+.2);hi=min(hi,run.end-.3)
  if hi<=lo:return
  result.append(dict(run=run.id,start=lo,end=hi,camera=camera,title=title,caption=caption,panel=panel,speed=speed))
 if num=='01':
  op,run=runs
  add(op,op.start,op.start+8,'customer_wide','01 / Customer-карта','Town01 — исходная сцена. Внешнее поле и холмы — синтетические сценарные дополнения. Границы: 10 000 × 10 000 м по geometry_summary.','geometry')
  add(op,op.start+8,op.start+16,'tower','01 / Синтетическое дополнение','Башня: 15 заданных этажей; крыша 45,25 м над datum. Это сценарная геометрия, не геодезическая модель района заказчика.','geometry')
  at=op.operator_clock.get('monotonic_ns',int(op.start*1e9))/1e9
  add(op,at+3,at+30,'uav_focus','01 / НПУ MAVProxy → UAV2','Записано настоящее окно MAVProxy: пять SYSID, vehicle 2, безопасный REQUEST_MESSAGE 148 и фактический COMMAND_ACK.','operator')
  add(run,run.start,run.start+7,'overview','01 / Запуск полётной части','Этот run: make demo-customer DEMO_GUI=0. Загрузка сокращена. С записью пяти камер RTF ≈0,776: штатный real-time gate не пройден.','fleet')
  add(run,run.phase('arm_uav1')-1,run.phase('hold_all')+4,'uav_focus','01 / Взлёт пяти БАС','Пять настоящих SITL. Штатные команды проходят через десять UART и единый ns-3 Wi-Fi / Sionna runtime.','fleet')
  add(run,run.phase('auto_mode_mission_uavs'),run.phase('auto_mode_mission_uavs')+14,'overview','01 / Автономная миссия UAV1','UAV2–UAV5 удерживают позиции. UAV1 выполняет заранее загруженную миссию; её полёт при потере связи не означает доставку новых команд.','fleet')
  add(run,run.phase('obstructed_candidate')-4,run.phase('obstructed_candidate')+5,'obstacle','01 / Точка за препятствием','Положение взято из текущего Gazebo. Ракурс камеры сам по себе не доказывает физическую классификацию LOS/NLOS.','metrics')
  add(run,run.phase('land_all')-2,run.phase('landing_complete')+2,'uav_focus','01 / Посадка и разоружение','LAND и автоматическое разоружение всех пяти SITL. Armed state и heartbeat показаны по реально полученной телеметрии.','fleet')
 elif num=='02':
  run=runs[0];a=run.phase('auto_mode_mission_uavs');b=run.phase('land_all')
  add(run,run.phase('hold_all')-5,a+2,'uav_focus','02 / Пять БАС в текущем запуске','Один подвижный БАС и четыре контрольных. Профиль: native Wi-Fi 2412 МГц, 20 МГц, 10 dBm; распространение Sionna.','radio')
  split=run.phase('obstructed_candidate_transit')
  add(run,a+2,split,'uav_focus','02 / Открытая точка','CP→UAV1 и UAV1→CP; UAV2 — неподвижный контроль. Полезная принятая мощность отличается от total RSSI.','radio')
  obstruct=run.phase('obstructed_candidate')
  add(run,split,max(split,obstruct-12),'overview','02 / Безопасный коридор','Маршрут исполняется автономно. В таблице — последнее доступное измерение и его возраст; пустое значение не превращается в ноль.','radio')
  add(run,max(split,obstruct-12),run.phase('return_transit')+8,'obstacle','02 / Точка за препятствием','Полный outage в NLOS не обязателен. Название точки — геометрическое; классификация LOS текущим результатом модели не предоставляется.','radio')
  add(run,run.phase('return_transit')+8,b,'overview','02 / Возврат и наблюдаемая связь','Кэш 20 с / 10 м сохраняется. Реакция может запаздывать, короткое восстановление может быть пропущено; график не сдвигается к движению.','radio')
  add(run,b,b+12,'uav_focus','02 / Граница измерения','Энергетический SINR получен из native received energy; это не decoder-weighted SINR. BLER для выбранного Wi-Fi: not_applicable.','radio')
 elif num=='03':
  run=runs[0]
  add(run,run.start,run.end,'uav_focus','03 / UART и native доступ к среде','SERIAL1 — control, SERIAL2 — MAVLink payload, не видеопоток. ACK приходят от SITL. Доступ к среде и retries выполняет ns-3 Wi-Fi; Sionna моделирует распространение.','communications')
 elif num=='04':
  descriptions={'baseline':'Baseline без источника. Фактический приём и доставка записаны в этом run_id.',
   'continuous':'Источник iso, 1 мВт, 2412 МГц / 20 МГц: активность в native model time 3–5 с. Включение, последствия и восстановление сохранены без ускорения.',
   'direction_front':'Диаграмма tr38901, азимут −π/4. Отдельный статический front case; остальные параметры источника сохранены.',
   'direction_back':'Диаграмма tr38901, азимут 3π/4. Отдельный back run: это не непрерывный поворот устройства.',
   'pulsed':'Импульсный источник: период 0,01 с, duty cycle 0,5, активность 3–5 с. График показывает фактические native arrivals.',
   'sweep':'Перестройка 2412 / 2462 МГц каждые 0,5 с; интервал 3–5 с. Временная шкала взята из native событий.',
   'multiple':'Два источника: [10,10,2] и [30,10,2] м. Это только симуляция; метки и стрелки — визуальные аннотации без collision.',
   'nonoverlap':'Контроль: источник 2462 МГц, полезный канал 2412 МГц. Частотное перекрытие отсутствует; показан фактический результат.'}
  for run in runs:
   case=run.manifest['case'];add(run,run.start,min(run.end,run.start+26),'uav_focus','04 / '+case,descriptions[case],'sources')
  run=next(run for run in runs if run.manifest['case']=='continuous')
  add(run,run.start+5,run.start+27,'overview','04 / Native heatmaps, отдельный расчёт','Карты рассчитаны после записи для reference continuous case: x=0…140, y=−20…120 м, z=2 м, 8×8, t=4 с. Одинаковые шкалы; no-path клетки оставлены пустыми. SINR≥10 dB — условная доступность, не измеренный PDR.','maps')
 elif num=='05':
  for run in runs:
   case=run.manifest['case'];at=run.operator_clock.get('monotonic_ns',int(run.start*1e9))/1e9
   add(run,at+3,at+(56 if case=='tcp' else 30),'uav_focus','05 / Внешний '+case+' gateway','Программная проверка интерфейса. Физический FC не подключён. Аппаратный HitL не подтверждён. UAV1 в Gazebo не является цифровым двойником отсутствующего физического FC.','operator')
   stop=readjson(run.path/'metrics/no_bypass_summary.json').get('started_monotonic_ns')
   if stop:add(run,stop/1e9-2,stop/1e9+10.5,'uav_focus','05 / Остановка общего ns-3/Sionna','После остановки native процесса SITL, UART и Gazebo остаются запущены. Безопасные запросы не получают ответов; процессные снимки и no_bypass_summary сохранены.','external')
  run=runs[-1]
  add(run,run.start+3,run.start+24,'overview','05 / Operating envelope RC1','Результаты испытаний RC1 с исходными run_id. Это отдельные radio-only benchmarks: 1/5/16 STA. Они не означают 16 SITL и не наложены как live measurements этого запуска.','benchmarks')
  add(run,run.start+3,run.start+24,'overview','05 / Дальности и явные профили','Испытания RC1 при 500/1000/2000 м и 10 dBm дали outage: 100 offered / 0 delivered. Sionna, Friis и explicit hybrid подписаны по выполненным конфигурациям.','ranges')
  add(run,run.start+3,run.start+27,'uav_focus','05 / Поставка и ограничения','make demo-record SCENARIO=01…05; make demo-record-all; make demo-video; make stop. Hardware FC: blocked_external. Не hard real-time; кэш имеет задержку; простая модель ограничена; передача CAVISE требует отдельного согласования.','delivery')
 return result


def make_panel(image,draw,run,row,clip,t,output):
 x,y=1340,120;panel=clip['panel'];num=run.manifest['scenario'][:2]
 text(draw,(x,y),'ИЗ ЖУРНАЛА ЭТОГО ЗАПУСКА',18,'#79d9d0',True);y+=34
 text(draw,(x,y),'БАС       z, м    режим     armed / HB',20);y+=32
 for i in range(1,6):
  hbtime,hb=run.telemetry.get(('control',i),Series()).at(t)
  pos=row['positions'].get(f'uav{i}')
  mode={0:'STAB',3:'AUTO',4:'GUIDED',9:'LAND'}.get(hb.get('custom_mode'),'mode'+str(hb.get('custom_mode'))) if hb else '—'
  armed=('YES' if hb['base_mode']&128 else 'NO') if hb else '—'
  age=fmt(t-hbtime,1)+'с' if hbtime else '—'
  text(draw,(x,y),f'UAV{i}    {fmt(pos[2] if pos else None):>5}    {mode:<7} {armed} / {age}',19,COLORS[i-1]);y+=31
 y+=12
 if panel=='operator' and (run.path/'video/operator.mkv').stat().st_size:
  text(draw,(x,y),'НАСТОЯЩЕЕ ОКНО MAVProxy',20,'#f4cf70',True)
  return y+35
 if panel in ('geometry','benchmarks','ranges','delivery','maps'):
  return y
 if panel=='communications':
  text(draw,(x,y),'UART ACK: SERIAL1 / SERIAL2',21,'#79d9d0',True);y+=30
  for i in range(1,6):
   parts=[]
   for channel in ('control','payload'):
    at,ack=run.acks.get((channel,i),Series()).at(t);parts.append('ACK '+str(ack['result']) if ack else 'нет данных')
   text(draw,(x,y),f'UAV{i}:   {parts[0]} / {parts[1]}',20);y+=28
  operations=run.summary.get('command_operations',[])
  sent=sum(op['attempts'][0]['send_monotonic_ns']/1e9<=t for op in operations)
  received=sum(bool(op['attempts'][0].get('ack_gcs_received_monotonic_ns')) and op['attempts'][0]['ack_gcs_received_monotonic_ns']/1e9<=t for op in operations)
  text(draw,(x,y+8),f'Parallel one-shot: sent {sent} / ACK {received}',20,'#f4cf70');y+=45
  text(draw,(x,y),'P2P ↓/↑ RX   multicast RX   shared RX',18);y+=28
  for i in range(1,6):
   def count(k):return run.app.get((k,f'uav{i}'),Series()).count(t)
   text(draw,(x,y),f'UAV{i}:   {count("p2p_downlink")}/{count("up_rx")}             {count("p2mp_downlink")}              {count("shared_rx")}',20);y+=28
  roots=run.app.get(('roots','all'),Series()).count(t)
  text(draw,(x,y+6),f'Настоящих multicast roots: {roots}',20,'#79d9d0');y+=42
  delivered=sum(run.app.get(('p2mp_downlink',f'uav{i}'),Series()).count(t) for i in range(1,6))
  text(draw,(x,y),'P2MP application PDR: '+(f'{delivered/(5*roots):.3f}' if roots else 'нет данных'),20);y+=30
  actual=[op for op in operations if op['attempts'][0].get('ack_gcs_received_monotonic_ns') and op['attempts'][0]['ack_gcs_received_monotonic_ns']/1e9<=t]
  if actual:
   op=actual[-1];attempt=op['attempts'][0]
   text(draw,(x,y),f'Последний ACK {op["uav"]}: {(attempt["ack_gcs_received_monotonic_ns"]-attempt["send_monotonic_ns"])/1e6:.2f} мс',20);y+=30
 else:
  links=[('cp','uav1')] if panel=='sources' else [('cp','uav1'),('uav1','cp')]
  for tx,rx in links:
   at,energy=run.energy.get((tx,rx),Series()).at(t)
   age=t-at if at else None
   current=energy if at and age<=1.1 else {}
   pt,path=run.native_links.get((tx,rx),Series()).at(t)
   native=run.native_time(t)
   generation=number(details(path['details']).get('channel_generation_time_s')) if path else None
   channel_age=max(0,native-generation) if native is not None and generation is not None else None
   a=row['positions'].get(tx);b=row['positions'].get(rx)
   distance=math.dist(a,b) if a and b else None
   text(draw,(x,y),f'{tx.upper()} → {rx.upper()}    d={fmt(distance)} м',21,'#79d9d0',True);y+=30
   text(draw,(x,y),f'S={fmt(number(current.get("signal_dbm")))} dBm',20);y+=27
   text(draw,(x,y),f'RSSI={fmt(number(current.get("interval_mean_total_rssi_dbm")))} dBm',20);y+=27
   text(draw,(x,y),f'Energy SINR={fmt(number(current.get("energy_ratio_sinr_db")))} dB',20);y+=27
   text(draw,(x,y),f'Пути: {fmt(number(path["value"]) if path else None,0)}; возраст канала {fmt(channel_age)} с',18);y+=25
   text(draw,(x,y),f'Возраст измерения: {fmt(age)} с',18,'#a5b6cb');y+=34
   if panel=='sources':
    jammer=number(current.get('jammer_mean_w'))
    jammer_db=10*math.log10(jammer*1000) if jammer and jammer>0 else None
    js=number(current.get('js_linear'))
    js_db=10*math.log10(js) if js and js>0 else None
    text(draw,(x,y),f'J={fmt(jammer_db)} dBm; J/S={fmt(js_db)} dB',20);y+=29
    if jammer==0:text(draw,(x,y),'В этом интервале энергия помехи = 0 W',18,'#a5b6cb');y+=26
  if panel!='sources':
   ct,control=run.energy.get(('cp','uav2'),Series()).at(t)
   text(draw,(x,y),'Контроль UAV2: S='+fmt(number(control.get('signal_dbm')) if control and t-ct<1.1 else None)+' dBm',20,'#79dfad');y+=34
  if panel=='sources':
   source=run.sources[0] if run.sources else None
   if source:
    text(draw,(x,y),f'{source["center_hz"]/1e6:g} МГц / {source["bandwidth_hz"]/1e6:g} МГц',20,'#f4cf70');y+=28
    text(draw,(x,y),f'{source["power_w"]*1000:g} мВт; {source["pattern"]}; duty {source["duty_cycle"]}',20);y+=28
    text(draw,(x,y),f'Азимут {source["orientation_rad"][0]:.3f}; active 3…5 с',18);y+=28
   else:text(draw,(x,y),'Baseline: источников нет',20,'#f4cf70');y+=28
   hb=sum(series.count(t) for (channel,_),series in run.telemetry.items() if channel=='control')
   text(draw,(x,y),f'FC HEARTBEAT доставлено НПУ: {hb}',19);y+=27
   text(draw,(x,y),'UART PDR: нет offer/delivery denominator',16,'#a5b6cb');y+=24
 retries=run.native.get('wifi_data_retry',Series()).count(t)
 at,queue=run.native.get('wifi_mac_queue_enqueue',Series()).at(t)
 text(draw,(x,y+8),f'Native data retries: {retries}',20);y+=35
 if queue:text(draw,(x,y),'Последняя native queue: '+queue['value'],18);y+=28
 drops=run.native.get('phy_rx_drop',Series()).count(t);errors=run.native.get('phy_rx_error',Series()).count(t)
 text(draw,(x,y),f'PHY drops {drops}; decoder errors {errors}',18);y+=26
 if panel=='external':
  _,snapshot=run.runtime.at(t)
  if snapshot:
   text(draw,(x,y),'Возраст native heartbeat: '+fmt(snapshot.get('native_heartbeat_wall_age_s'))+' с',19);y+=28
   ep=snapshot.get('external_endpoint',{})
   text(draw,(x,y),f'Gateway radio_live: {ep.get("radio_live","нет данных")}',19)
  probe=readjson(run.path/'metrics/no_bypass_summary.json')
  if t>=probe.get('started_monotonic_ns',float('inf'))/1e9:
   ps=(run.path/'logs/processes_native_stopped.txt').read_text()
   sitl=len(re.findall(r'\sarducopter --model ',ps))
   uart=ps.count('communication_vertical.py uart-adapter ')
   draw.rectangle((40,645,1290,816),fill='#12263a')
   text(draw,(56,657),'Наблюдаемые процессы после остановки native ns-3/Sionna',23,'#f4cf70',True)
   text(draw,(56,700),f'SITL: {sitl}; UART adapters: {uart}; внешний gateway: {"external_endpoint.py" in ps}; Gazebo: {"gz sim -v4" in ps}',23)
   text(draw,(56,751),'Источник: processes_native_stopped.txt; проверка обмена: no_bypass_summary.json',21)
 return None


def trace_plot(image,run,t,panel):
 if panel not in ('sources','radio','metrics'):return
 draw=ImageDraw.Draw(image);x0,y0,x1,y1=1340,855,1885,1005
 draw.rectangle((x0,y0,x1,y1),fill='#111f32',outline='#34465e')
 text(draw,(x0+8,y0+3),'Реальные arrivals: S / J, dBm; wall −12…0 с',16,'#a5b6cb')
 lo=t-12;ploty=y0+28;bottom=y1-12
 for val in (-100,-80,-60,-40):
  yy=bottom-(val+110)/100*(bottom-ploty)
  draw.line((x0+35,yy,x1-5,yy),fill='#263b53');text(draw,(x0+2,yy-8),str(val),12,'#a5b6cb')
 series=run.energy.get(('cp','uav1'),Series());a=bisect.bisect_left(series.times,lo);b=bisect.bisect_right(series.times,t)
 for field,color in [('signal_dbm','#68d9d0'),('jammer_mean_w','#f2bd67')]:
  previous=None
  for at,row in zip(series.times[a:b],series.values[a:b]):
   val=number(row.get(field))
   if field=='jammer_mean_w':val=10*math.log10(val*1000) if val and val>0 else None
   if val is None:previous=None;continue
   point=(x0+36+(at-lo)/12*(x1-x0-42),bottom-(max(-110,min(-10,val))+110)/100*(bottom-ploty))
   if previous and at-previous[0]<1.1:draw.line([previous[1],point],fill=color,width=2)
   else:draw.ellipse((point[0]-1,point[1]-1,point[0]+1,point[1]+1),fill=color)
   previous=(at,point)
 a=bisect.bisect_left(run.interference.times,lo);b=bisect.bisect_right(run.interference.times,t)
 for at,row in zip(run.interference.times[a:b],run.interference.values[a:b]):
  val=number(row['value'])
  if val is None:continue
  xx=x0+36+(at-lo)/12*(x1-x0-42);yy=bottom-(max(-110,min(-10,val))+110)/100*(bottom-ploty)
  draw.line((xx,yy-2,xx,yy+2),fill='#f2bd67',width=1)
 text(draw,(x0,1012),'Разрывы не сглажены; при outage S может отсутствовать.',16,'#a5b6cb')
 if panel=='sources':
  sx,sy,sw=44,664,1230
  draw.rectangle((sx,sy,sx+sw,sy+142),fill='#122337')
  text(draw,(sx+12,sy+8),'События native источника: частота и включение; wall −12…0 с',22,'#f4cf70',True)
  switches=[]
  for kind in ('jammer_on','jammer_off'):
   series=run.native.get(kind,Series())
   switches.extend((at,kind,row) for at,row in zip(series.times,series.values) if at<=t and row['node']=='source1')
  switches.sort(key=lambda item:(item[0],0 if item[1]=='jammer_off' else 1))
  for i,(at,kind,row) in enumerate(switches):
   stop=switches[i+1][0] if i+1<len(switches) else t
   if kind!='jammer_on' or stop<lo:continue
   center=number(details(row['details']).get('center_hz'))
   x=sx+12+max(0,(at-lo)/12)*(sw-24);end=sx+12+min(1,(stop-lo)/12)*(sw-24)
   if end>x:
    draw.rectangle((x,sy+48,end,sy+91),fill='#397f8c' if center==2412000000 else '#966b2d')
    if end-x>40:text(draw,(x+2,sy+58),fmt(center/1e6 if center else None,0),15)
  text(draw,(sx+12,sy+109),'Тёмный участок: нет зарегистрированного включения. Импульсы приёма — на графике J справа.',18,'#a5b6cb')


def annotation(image,run,row,camera):
 draw=ImageDraw.Draw(image)
 if camera not in ('customer_wide','tower'):
  for name,pos in row['positions'].items():
   point=project(pos,camera)
   if point:
    px,py=point;color='#69bdfa' if name=='cp' else COLORS[int(name[-1])-1]
    draw.ellipse((px-9,py-9,px+9,py+9),outline='#101922',width=5)
    draw.ellipse((px-7,py-7,px+7,py+7),outline=color,width=2)
    label='НПУ' if name=='cp' else name.upper()
    draw.text((px+11,py-25),label,font=ImageFont.truetype(BOLD,20),fill=color,stroke_width=2,stroke_fill='#101922')
  for source in run.sources:
   point=project(source['position_m'],camera)
   if point:
    px,py=point;draw.polygon([(px,py-14),(px-13,py+10),(px+13,py+10)],fill='#f5c365',outline='#101922')
    draw.text((px+15,py-10),source['id']+' (метка)',font=ImageFont.truetype(BOLD,18),fill='#f5c365',stroke_width=2,stroke_fill='#101922')
    az=source['orientation_rad'][0];q=project([source['position_m'][0]+6*math.cos(az),source['position_m'][1]+6*math.sin(az),source['position_m'][2]],camera)
    if q:draw.line([point,q],fill='#f5c365',width=3)


def static_panel(image,draw,run,clip,output,y,t):
 x=1340;panel=clip['panel']
 if panel=='geometry':
  g=readjson(output/'reports/geometry_summary.json')
  lo=g.get('external_bounds_min_m',[0,0,0])[2];hi=g.get('external_bounds_max_m',[0,0,0])[2]
  wrap(draw,(x,y),'10 000 × 10 000 м\nTown01: исходная геометрия.\nВнешняя mesh — синтетическая.',33,24)
  wrap(draw,(x,y+155),f'Внешняя mesh z: {lo:.3f}…{hi:.3f} м; перепад {hi-lo:.3f} м. Источник: geometry_summary.json.',33,23)
 elif panel in ('benchmarks','ranges'):
  ref=readjson(output/'reports/rc1_reference/rc1-native-reference-matrix/summary.json')
  text(draw,(x,y),'РЕЗУЛЬТАТЫ ИСПЫТАНИЙ RC1',20,'#f4cf70',True);y+=40
  text(draw,(x,y),'rc1-native-reference-matrix',19);y+=38
  for case in ref.get('cases',[]):
   isrange=not case['name'].startswith('radio_')
   if isrange!=(panel=='ranges'):continue
   text(draw,(x,y),case['name'],21,'#79d9d0');y+=29
   text(draw,(x,y),f'{case.get("delivered_packets")} / {case.get("offered_packets")} доставок',20);y+=29
   if panel!='ranges':
    text(draw,(x,y),f'S={fmt(case.get("mean_signal_dbm"))} dBm; wall {case["wall_duration_s"]:.2f} с',18);y+=36
  wrap(draw,(x,936),'Radio-only; не live метрики текущего Gazebo.',36,19,'#f4cf70')
 elif panel=='delivery':
  items=['Локальная поставка RC1','source.bundle + runtime image','pinned dependencies + assets','USER_GUIDE и отчёты','Демопакет: 5 MP4 + общий фильм','raw, SRT, time mapping','hardware FC: blocked_external','Не hard real-time','20 с / 10 м: задержка кэша','Friis/hybrid: только явный профиль','CAVISE: условия передачи отдельно']
  for item in items:text(draw,(x,y),item,21,'#f4cf70' if 'blocked' in item else '#eaf0f7');y+=38
 elif panel=='maps':
  maps=output/'reports/native-maps/heatmaps'
  phase=['baseline','jammer','delta'][min(2,int((t-clip['start'])/(clip['end']-clip['start'])*3))]
  text(draw,(x,y),phase+' / SINR, dB',24,'#f4cf70',True);y+=40
  im=Image.open(maps/(phase+'_sinr_db.png')).convert('RGB');im.thumbnail((548,400));image.paste(im,(x,y));y+=412
  ref=readjson(output/'reports/native-maps-run.json')
  wrap(draw,(x,y),'Отдельный расчёт: '+ref.get('run_id','нет данных'),39,19,'#a5b6cb')
  wrap(draw,(x,955),'8×8; z=2 м; t=4 с. Одинаковые шкалы baseline/jammer. Не карта PDR.',39,19,'#f4cf70')


def render_clip(clip,run,config,output,index):
 camera=clip['camera'];start=clip['start'];end=clip['end'];speed=clip['speed']
 duration=(end-start)/speed;nframes=int(round(duration*FPS))
 first=run.frame(camera,start);firstidx=first['frame']
 decode=Decoder(run.path/'video'/(camera+'.avi'),firstidx/25)
 operator=None;opstart=run.operator_clock.get('monotonic_ns',0)/1e9
 if clip['panel']=='operator' and (run.path/'video/operator.mkv').stat().st_size:
  operator=Decoder(run.path/'video/operator.mkv',max(0,start-opstart))
 segment=output/'videos/segments'/f'{config["id"]}-{index:02}.mp4';segment.parent.mkdir(exist_ok=True)
 encoder=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','rawvideo','-pix_fmt','rgb24','-s','1920x1080','-r','25','-i','-',
   '-an','-c:v','libx264','-preset','veryfast','-crf','21','-threads','6','-pix_fmt','yuv420p','-movflags','+faststart',str(segment)],stdin=subprocess.PIPE)
 try:
  for n in range(nframes):
   t=start+n/FPS*speed;row=run.frame(camera,t);raw=decode.get(row['frame']-firstidx)
   if raw is None:raise RuntimeError('Raw video decode failed: '+run.id)
   image=Image.new('RGB',(W,H),'#0c1625');image.paste(raw,(24,114));draw=ImageDraw.Draw(image)
   text(draw,(24,18),'BAS v2 RC1  /  '+clip['title'],32,bold=True)
   rtf=run.interval_rtf(camera,row);native=run.native_time(t)
   text(draw,(24,66),f'Gazebo {row["simulation_time_s"]:.2f} с   |   wall +{t-run.start:.2f} с   |   RTF Δ2с {fmt(rtf,3)}   |   native {fmt(native,2)} с',22,'#79d9d0')
   text(draw,(1340,71),f'Захват: {run.performance["cameras"][camera]["actual_fps"]:.1f} кадр/с; файл 25 FPS',20,'#f4cf70')
   text(draw,(24,846),'Фаза: '+row['phase']+'  |  run_id: '+run.id,21,'#a5b6cb')
   text(draw,(24,880),'Метрики из журнала этого запуска, синхронизированы по времени',22,'#79d9d0')
   wrap(draw,(24,922),clip['caption'],width=88,size=25)
   annotation(image,run,row,camera)
   y=make_panel(image,draw,run,row,clip,t,output)
   if operator and y:
    op=operator.get(n)
    if op:
     op=op.crop((26,76,op.width-26,op.height-28));op.thumbnail((556,540));image.paste(op,(1340,y))
   if clip['panel'] in ('geometry','benchmarks','ranges','delivery','maps'):static_panel(image,draw,run,clip,output,y,t)
   trace_plot(image,run,t,clip['panel'])
   if clip['panel']=='communications' and t>=run.phase('communications_complete',math.inf):
    shared=readjson(run.path/'metrics/shared_medium_summary.json')
    draw.rectangle((40,629,1290,817),fill='#12263a')
    text(draw,(56,642),'Shared uplink: goodput за завершённое окно опыта',24,'#79d9d0',True)
    for i in range(1,6):
     metrics=shared.get('per_uav',{}).get(f'uav{i}',{}).get('application_metrics',{})
     text(draw,(56+(i-1)*240,688),f'UAV{i}: '+fmt(metrics.get('goodput_bps'),0)+' бит/с',18,COLORS[i-1])
     text(draw,(56+(i-1)*240,722),'PDR '+fmt(metrics.get('pdr'),3),19)
    text(draw,(56,770),'Jain fairness: '+fmt(shared.get('jain_fairness'),3)+'; окно: первый offer → последний offer/receipt',20)
   if config['id'].startswith('05') and clip['panel']=='operator':
    _,snapshot=run.runtime.at(t)
    if snapshot:
     ep=snapshot.get('external_endpoint',{})
     draw.rectangle((40,633,1290,744),fill='#12263a')
     text(draw,(56,646),f'Gateway: connected={ep.get("connected", "нет данных")}; reconnects={ep.get("reconnects", "нет данных")}',24,'#79d9d0',True)
     text(draw,(56,687),f'radio_live={ep.get("radio_live", "нет данных")}; возраст native heartbeat {fmt(snapshot.get("native_heartbeat_wall_age_s"),2)} с',23)
    draw.rectangle((24,755,1304,834),fill='#3c2027')
    text(draw,(38,764),'Программная проверка интерфейса. Физический FC не подключён.',23,'#ffe0bc',True)
    text(draw,(38,799),'Аппаратный HitL не подтверждён.',22,'#ffe0bc')
   at,lag=run.native.get('realtime_lag',Series()).at(t)
   _,solve=run.solves.at(t)
   text(draw,(1340,1043),'lag '+fmt(number(lag['value']) if lag else None,2)+' мс; solve '+fmt(solve,2)+' мс',19,'#f4cf70')
   if speed!=1:text(draw,(1080,126),f'ВРЕМЯ ×{speed:g}',23,'#f4cf70',True)
   if n==nframes//2:
    image.save(output/'screenshots'/f'{config["id"]}-{index:02}.jpg',quality=92)
   encoder.stdin.write(image.tobytes())
 finally:
  decode.close()
  if operator:operator.close()
  encoder.stdin.close()
  code=encoder.wait()
 if code:raise RuntimeError('H.264 encoder failed')
 return segment,nframes/FPS


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--scenario',default='all');p.add_argument('--plan-only',action='store_true');p.add_argument('--preview-only',action='store_true');a=p.parse_args();a.output=a.output.resolve()
 configs=[json.loads(p.read_text()) for p in sorted((ROOT/'network/config/demo').glob('*.json'))]
 timeline=[];all_run_data={}
 for config in configs:
  num=config['id'][:2]
  if a.scenario not in ('all',num):continue
  manifests=readjson(a.output/'reports'/(config['id']+'-runs.json'),[])
  if not manifests:raise RuntimeError('Scenario has not been recorded: '+config['id'])
  runs=[Run(a.output/'raw'/m['run_id']) for m in manifests]
  lookup={run.id:run for run in runs};all_run_data.update(lookup)
  plan=clips(config,runs)
  (a.output/'reports'/(config['id']+'-edit.json')).write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
  print('EDIT',config['id'],len(plan),'clips',sum((s['end']-s['start'])/s['speed'] for s in plan),'seconds',flush=True)
  if a.plan_only:continue
  if a.preview_only:
   for i in sorted({0,len(plan)//2,len(plan)-1}):
    clip=dict(plan[i]);clip['start']=(clip['start']+clip['end'])/2;clip['end']=clip['start']+.04
    render_clip(clip,lookup[clip['run']],config,a.output,i)
   continue
  paths=[];elapsed=0;srt=[]
  for i,clip in enumerate(plan):
   segment,duration=render_clip(clip,lookup[clip['run']],config,a.output,i);paths.append(segment)
   run=lookup[clip['run']]
   timeline.append(dict(scenario=config['id'],source_run_id=run.id,source_file=str(run.path/'video'/(clip['camera']+'.avi')),
      source_start=run.frame(clip['camera'],clip['start'])['frame']/25,source_end=run.frame(clip['camera'],clip['end'])['frame']/25,
      source_clock='AVI frame index/25; wall clock in source_*_monotonic_s and frames.jsonl',
      source_start_monotonic_s=clip['start'],source_end_monotonic_s=clip['end'],final_video_start=elapsed,final_video_end=elapsed+duration,
      speed=clip['speed'],overlay_source=str(run.path/'metrics'),title=clip['title']))
   # Repeat explanation in short readable subtitle cues without changing source time.
   for j in range(max(1,math.ceil(duration/10))):
    lo=elapsed+j*10;hi=min(elapsed+duration,lo+10)
    if hi>lo:srt.append(f'{len(srt)+1}\n{tc(lo).replace(".",",")} --> {tc(hi).replace(".",",")}\n'+'\n'.join(textwrap.wrap(clip['caption'],85))+'\n')
   elapsed+=duration
   print('ENCODED',segment.name,round(duration,2),flush=True)
  sub=a.output/'subtitles'/(config['id']+'.srt');sub.write_text('\n'.join(srt))
  listing=a.output/'reports'/(config['id']+'-concat.txt');listing.write_text(''.join("file '"+str(path).replace("'","'\\''")+"'\n" for path in paths))
  subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listing),'-c','copy','-movflags','+faststart',str(a.output/'videos'/(config['id']+'.mp4'))],check=True)
 if not a.plan_only and not a.preview_only:
  path=a.output/'edit_timeline.csv'
  if a.scenario!='all' and path.exists():
   prior=list(csv.DictReader(path.open()));timeline=[r for r in prior if not r['scenario'].startswith(a.scenario)]+timeline
  with path.open('w') as stream:
   writer=csv.DictWriter(stream,fieldnames=list(timeline[0]));writer.writeheader();writer.writerows(timeline)
 return 0
if __name__=='__main__':raise SystemExit(main())
