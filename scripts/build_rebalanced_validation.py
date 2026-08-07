import json,re,sys
from pathlib import Path
project=Path(__file__).resolve().parents[1];sys.path.insert(0,str(project))
from backend.adapters.registry import get_adapter
root=project/'training/llamafactory/v1'; release=root/'release_v1'
targets={'mysql_police_address':12,'gauss_ecommerce':5,'dameng_ecommerce':4,'postgres_stock':3,'hive_hadoop_demo':3,'countries_graphql':2,'sqlite_demo':1}
instruction='根据给定数据源元数据，将用户问题转换为一条只读 SQL。只使用给出的表和字段；仅输出 SQL，不要解释、不要 Markdown、不要输出多条语句。'
def src(r):return re.search('数据源：([^\n]+)',r['input']).group(1)
def norm(x):return re.sub(r'[^0-9a-zA-Z\u4e00-\u9fff]+','',x).lower()
train=json.loads((release/'train/nl2sql_topic_sft_rebalanced_v1.json').read_text(encoding='utf8'))
old=[]
for f in ['nl2sql_topic_validation_seed_v1.json','nl2sql_topic_validation_batch2_mysql_police_v1.json']:old+=json.loads((release/'validation'/f).read_text(encoding='utf8'))
seen={norm(r['output']) for r in train};out=[]
for s,n in targets.items():
 for r in old:
  if src(r)==s and sum(src(x)==s for x in out)<n and norm(r['output']) not in seen:out.append(r);seen.add(norm(r['output']))
 need=n-sum(src(x)==s for x in out);a=get_adapter(s)
 if need:
  for t in a.get_metadata()['tables']:
   for c in t['columns']:
    col=c['name']
    if col.lower() in {'name','email','phone','id_no','alert_content','full_address','short_address','description'}:continue
    sql=f'SELECT COUNT(DISTINCT {col}) AS distinct_value_count FROM {t["name"]}'
    if norm(sql) in seen:continue
    try:a.execute_query(sql)
    except Exception:continue
    out.append({'instruction':instruction,'input':f'数据源：{s}\nSQL 方言：目标数据源方言\n表：{t["name"]}({col})。\n用户问题：统计 {t["name"]} 表中 {col} 字段的不重复取值数量。','output':sql+';'});seen.add(norm(sql));need-=1
    if not need:break
   if not need:break
 if need:raise RuntimeError(f'{s} shortage')
if len(out)!=30:raise RuntimeError(len(out))
(release/'validation/nl2sql_topic_validation_rebalanced_v1.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps({s:sum(src(r)==s for r in out) for s in targets},ensure_ascii=False))
