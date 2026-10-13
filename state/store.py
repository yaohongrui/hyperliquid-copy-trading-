import json, os, tempfile
from dataclasses import asdict
from pathlib import Path
class StateStore:
 def __init__(self,path): self.path=Path(path)
 def load(self):
  try: return json.loads(self.path.read_text())
  except (FileNotFoundError,json.JSONDecodeError): return {}
 def save(self,data):
  self.path.parent.mkdir(parents=True,exist_ok=True)
  fd,tmp=tempfile.mkstemp(dir=self.path.parent,prefix='.state-',text=True)
  try:
   with os.fdopen(fd,'w') as f: json.dump(data,f,indent=2,sort_keys=True); f.flush(); os.fsync(f.fileno())
   os.replace(tmp,self.path)
  finally:
   if os.path.exists(tmp): os.unlink(tmp)
