"""파일 통돌이 실제 촬영용 합성 파일 100,000개. 기존 출력은 덮어쓰지 않는다."""
from __future__ import annotations
import argparse, collections, datetime as dt, hashlib, io, json, os, struct, time, zlib
from pathlib import Path
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from PIL import Image,ImageDraw

COUNTS={'md':22000,'txt':16000,'csv':10000,'json':8000,'py':12000,'js':8000,'log':8000,'png':6000,'pdf':4000,'docx':2500,'xlsx':2000,'pptx':500,'key':1000}
NODES=['01.착수/01.과제수행계획서','01.착수/02.착수보고서','02.분석/01.요구사항정의서','02.분석/02.현행분석','03.설계/01.화면설계서','03.설계/02.인터페이스설계서','04.구현/01.개발가이드','05.테스트/01.테스트시나리오','05.테스트/02.테스트결과서','06.이행/01.이행계획서','07.운영/01.운영매뉴얼','08.보고/01.주간보고','08.보고/02.회의록']
ROOTS=[('기존 과제 폴더','Documents/기존과제_자료',30000),('개발 폴더','Development/가상플랫폼_개발',40000),('다운로드 폴더','Downloads',30000)]

def pdf():
 objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
 content=b'BT /F1 18 Tf 60 760 Td (SYNTHETIC DEMO - Platform project document) Tj 0 -30 Td (All content is fictional. No company or personal data.) Tj ET';objects.append(b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream');out=b'%PDF-1.4\n';offsets=[0]
 for i,o in enumerate(objects,1):offsets.append(len(out));out+=f'{i} 0 obj\n'.encode()+o+b'\nendobj\n'
 pos=len(out);out+=f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()+b''.join(f'{v:010} 00000 n \n'.encode() for v in offsets[1:]);out+=f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{pos}\n%%EOF\n'.encode();return out

def templates():
 result={'pdf':pdf()}
 d=Document();d.add_heading('가상플랫폼 개발 과제 · 촬영용 합성 문서',0);d.add_paragraph('실제 고객, 회사, 개인정보가 없는 가상의 산출물입니다. 요구사항 검토와 테스트 결과를 기록합니다.');b=io.BytesIO();d.save(b);result['docx']=b.getvalue()
 w=Workbook();s=w.active;s.title='가상요구사항';s.append(['ID','구분','항목','상태']);s.append(['MOCK-001','기능','가상 로그인 조회','검토']);b=io.BytesIO();w.save(b);result['xlsx']=b.getvalue()
 p=Presentation();sl=p.slides.add_slide(p.slide_layouts[1]);sl.shapes.title.text='가상플랫폼 개발 자료';sl.placeholders[1].text='촬영용 합성 데이터\n실제 회사·고객·개인 자료 없음';b=io.BytesIO();p.save(b);result['pptx']=b.getvalue()
 im=Image.new('RGB',(640,360),'#E7EFFB');dr=ImageDraw.Draw(im);dr.rectangle((30,30,610,330),outline='#2F6FDB',width=4);dr.text((55,55),'SYNTHETIC PLATFORM UI\nDEMO DATA ONLY',fill='#15294A');b=io.BytesIO();im.save(b,format='PNG');result['png']=b.getvalue();return result

def payload(ext,uid,title,tpl):
 if ext=='pdf':return tpl[ext]+f'\n% mock-id:{uid}\n'.encode()
 if ext in ('docx','xlsx','pptx'):
  # ZIP comments distinguish synthetic files without invalidating office document formats.
  b=bytearray(tpl[ext]);pos=b.rfind(b'PK\x05\x06');comment=f'MOCK-{uid}'.encode();b[pos+20:pos+22]=struct.pack('<H',len(comment));return bytes(b[:pos+22])+comment
 if ext=='png':
  b=tpl[ext];t=b'SyntheticId\0'+uid.encode();chunk=struct.pack('>I',len(t))+b'tEXt'+t+struct.pack('>I',zlib.crc32(b'tEXt'+t)&0xffffffff);return b[:-12]+chunk+b[-12:]
 if ext=='json':return json.dumps({'mock':True,'id':uid,'project':'가상플랫폼_2026','document':title,'status':'검토'},ensure_ascii=False).encode()
 if ext=='csv':return f'id,project,task,status\n{uid},MOCK_PLATFORM,{title},review\n'.encode()
 if ext in ('py','js'):return (f'# MOCK {uid}\n# Synthetic development sample\ndef sample_{uid}():\n    return "mock-only"\n' if ext=='py' else f'// MOCK {uid}\nexport const sample = {{id:"{uid}",mock:true}};\n').encode()
 if ext=='key':return f'FAKE-DEMO-NOT-A-KEY\nsynthetic-id={uid}\n'.encode()
 if ext=='log':return f'2026-10-01 INFO MOCK-{uid} synthetic platform test completed\n'.encode()
 return f'# {title}\n\n가상플랫폼_2026 촬영용 합성 데이터입니다.\n문서 ID: {uid}\n실제 회사·고객·개인정보는 포함하지 않습니다.\n검토 항목: 로그인, 자료 검색, 승인 후 파일 이동, 처리 기록 확인.\n'.encode()

def generate(out):
 if out.exists():raise SystemExit('기존 경로는 보존합니다. 새 --out 경로를 지정하세요.')
 home=out/'home';home.mkdir(parents=True);tpl=templates();exts=[k for k,v in COUNTS.items() for _ in range(v)];# Deterministic interleaving across roots.
 import random;random.Random(261122).shuffle(exts)
 manifest=[];uid=0;now=time.time();special={}
 for label,root,count in ROOTS:
  base=home/root
  for j in range(count):
   ext=exts[uid];uid+=1;key=f'{uid:06d}';node=NODES[(uid*7)%len(NODES)];kind='로컬 분류 후보' if uid%10<7 else '확인·묶음 후보'
   title=node.split('/')[-1].split('.',1)[-1] if uid%10<7 else ['검토자료','참고메모','분류전자료'][uid%3]
   parent=base/f'묶음_{j%40:02d}'/f'세트_{(j//40)%10:02d}';name=f'{title}_{key}.{ext}'
   # Deliberately ambiguous files sort first in the local review batch.
   if root=='Downloads' and j==0:ext='md';exts[uid-1]='md';name='인증토큰_발급_API_연계흐름.md';title='API 연계 흐름';special['names']=str((parent/name).relative_to(home))
   if root=='Downloads' and j==1:ext='md';exts[uid-1]='md';name='자료_A17_검토.md';title='확인 필요 자료';special['body']=str((parent/name).relative_to(home))
   # Keep these files outside bulk source groups so Liner selection stays explicit.
   if root=='Downloads' and j in (0,1):parent=base
   parent.mkdir(parents=True,exist_ok=True);p=parent/name
   data=payload(ext,key,title,tpl)
   if root=='Downloads' and j==0:
    data='''# 가상플랫폼_2026 인증 API 연계 흐름
서비스 간 인증 토큰 발급 요청과 응답을 정의하는 인터페이스 설계 초안입니다.
POST /mock/token 요청은 client_id와 request_id를 포함하며, 응답은 가상 토큰과 만료 시간을 반환합니다.
외부 기관이나 실제 서비스의 키·고객 정보는 없습니다. API 오류 코드와 재시도 조건을 비교합니다.
'''.encode()
   if root=='Downloads' and j==1:
    data='''# 가상플랫폼_2026 요구사항 정의서
이 문서는 가상플랫폼_2026의 기능 및 비기능 요구사항을 정의합니다.
FR-001 사용자는 파일 목록을 검색하고 종류별로 검사 대상을 선택할 수 있어야 합니다.
FR-002 시스템은 파일명과 폴더 경로를 우선 대조하고 불확실한 문서를 확인 목록으로 분리해야 합니다.
FR-003 선택한 문서만 외부 분석에 전송하며 사용자가 전송 범위를 확인하고 동의해야 합니다.
FR-004 사용자는 이동할 파일과 생성할 폴더를 승인한 후 파일을 이동할 수 있어야 합니다.
FR-005 실행 결과와 건너뜀 사유를 기록하고 변경되지 않은 파일을 되돌릴 수 있어야 합니다.
NFR-001 동일 이름의 기존 파일을 덮어쓰지 않아야 합니다.
검수 기준: 각 요구사항 ID마다 테스트 항목, 담당 역할, 완료 여부를 추적합니다.
실제 회사·고객·계정·개인정보가 없는 촬영용 가상 문서입니다.
'''.encode()
   if uid%50==0 and ext in ('md','txt'):
    data=b'# Synthetic identical duplicate\nMock project shared meeting record.\n';kind='중복 후보'
   p.write_bytes(data);age=(1 if root=='Downloads' and j<20 else 7+uid%330);stamp=now-age*86400-j%3600;os.utime(p,(stamp,stamp))
   manifest.append({'path':str(p.relative_to(home)),'root':label,'ext':ext,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'expected':kind})
  print(label,count,flush=True)
 dest=home/'Documents/가상플랫폼_2026'
 for node in NODES[:5]+[NODES[-1]]:(dest/node).mkdir(parents=True,exist_ok=True)
 # Correct special paths after placing them at Downloads root.
 special={k:'Downloads/'+Path(v).name for k,v in special.items()}
 (out/'계획한_폴더목록.txt').write_text('\n'.join(n+(' [필수]' if i in (0,2,4,5,7,8,9,10) else '') for i,n in enumerate(NODES)))
 (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,separators=(',',':')))
 summary={'brand':'파일 통돌이','generated_at':dt.datetime.now().isoformat(),'total_files':len(manifest),'roots':dict(collections.Counter(x['root'] for x in manifest)),'extensions':dict(collections.Counter(x['ext'] for x in manifest)),'bytes':sum(x['bytes'] for x in manifest),'special_files':special,'home':str(home.resolve()),'destination':'Documents/가상플랫폼_2026','original_files_touched':False,'mock_only':True}
 (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));(out/'.file_tongdory_recording').write_text('synthetic recording dataset v1\n')
 on_disk=sum(1 for p in home.rglob('*') if p.is_file());assert on_disk==100000==len(manifest),(on_disk,len(manifest));print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__':
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();generate(a.out.resolve())
