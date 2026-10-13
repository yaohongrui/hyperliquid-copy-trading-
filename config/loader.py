import os,re,yaml

def _load_env(path):
    try:
        for line in open(path):
            line=line.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            key,value=line.split('=',1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass

def load(path):
    path=os.path.abspath(path)
    _load_env(os.path.join(os.path.dirname(path),'lead.env'))
    text=open(path).read()
    text=re.sub(r'\$\{([^}]+)\}',lambda m:os.getenv(m.group(1),m.group(0)),text)
    return yaml.safe_load(text)
