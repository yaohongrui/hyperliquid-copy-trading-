import argparse
from config.loader import load
from core.symbols import normalize


def _validate_symbol_map(value):
    if value is None:
        return
    assert isinstance(value, dict), "symbol_map must be a mapping"
    for key, target in value.items():
        assert isinstance(key, str) and key.strip(), "symbol_map keys must be non-empty strings"
        assert normalize(key), f"symbol_map key has no base asset: {key!r}"
        assert target is None or isinstance(target, str), f"symbol_map[{key!r}] must be a string or null"


def validate(path):
 c=load(path); assert isinstance(c,dict); assert c.get('leaders') is not None; assert c.get('execution_accounts') is not None
 _validate_symbol_map(c.get('symbol_map'))
 names={a['name'] for a in c['leaders']}; accounts=set(c['execution_accounts'])
 for a in c['leaders']:
  assert a.get('execution_account') in accounts, f"unknown execution account: {a.get('execution_account')}"
 return True
if __name__=='__main__':
 p=argparse.ArgumentParser(); p.add_argument('config'); validate(p.parse_args().config); print('valid')
