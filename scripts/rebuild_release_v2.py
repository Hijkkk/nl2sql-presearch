import json,re,sys
from pathlib import Path
P=Path(__file__).resolve().parents[1];sys.path.insert(0,str(P))
from backend.adapters.registry import get_adapter
src=P/'training/llamafactory/v1/release_v1';out=P/'training/llamafactory/v1/release_v2'
targets={'mysql_police_address':80,'gauss_ecommerce':30,'dameng_ecommerce':30,'postgres_stock':20,'hive_hadoop_demo':20,'countries_graphql':10,'sqlite_demo':10}; vt={'mysql_police_address':8,'gauss_ecommerce':3,'dameng_ecommerce':3,'postgres_stock':2,'hive_hadoop_demo':2,'countries_graphql':1,'sqlite_demo':1};ins='根据给定数据源元数据，将用户问题转换为一条只读 SQL。只使用给出的表和字段；仅输出 SQL，不要解释、不要 Markdown、不要输出多条语句。'
def s(r):return re.search('数据源：([^\n]+)',r['input']).group(1)
def n(x):return re.sub(r'[^0-9a-zA-Z\u4e00-\u9fff]+','',x).lower()
train=json.loads((src/'train/nl2sql_topic_sft_rebalanced_v1.json').read_text(encoding='utf8'));val=sum((json.loads(p.read_text(encoding='utf8')) for p in (src/'validation').glob('*.json')),[])
def fill(records,goal,seen):
 for k,v in goal.items():
  need=v-sum(s(r)==k for r in records)
  a=get_adapter(k);m=a.get_metadata()
  for t in m['tables']:
   for c in t['columns']:
    col=c['name'];sql=f'SELECT {col}, COUNT(*) AS record_count FROM {t["name"]} GROUP BY {col} ORDER BY record_count DESC'
    if n(sql) in seen or col.lower() in {'name','email','phone','id_no','description','full_address','alert_content'}:continue
    try:a.execute_query(sql)
    except:continue
    records.append({'instruction':ins,'input':f'数据源：{k}\nSQL 方言：目标数据源方言\n表：{t["name"]}({col})。\n用户问题：按 {t["name"]} 表的 {col} 字段统计记录数量，并按数量降序排列。','output':sql+';'});seen.add(n(sql));need-=1
    if not need:break
   if not need:break
  if need:raise RuntimeError(k)
seen={n(r['output']) for r in train};fill(train,targets,seen)
val2=[];seen|={n(r['output']) for r in val}
for k,v in vt.items():val2 += [r for r in val if s(r)==k][:v]
fill(val2,vt,seen)
if len(train)!=200 or len(val2)!=20:raise RuntimeError((len(train),len(val2)))
(out/'train').mkdir(parents=True);(out/'validation').mkdir();(out/'train/train.json').write_text(json.dumps(train,ensure_ascii=False,indent=2),encoding='utf8');(out/'validation/validation.json').write_text(json.dumps(val2,ensure_ascii=False,indent=2),encoding='utf8');(out/'dataset_info.json').write_text(json.dumps({'nl2sql_train_v2':{'file_name':'train/train.json','columns':{'prompt':'instruction','query':'input','response':'output'}},'nl2sql_validation_v2':{'file_name':'validation/validation.json','columns':{'prompt':'instruction','query':'input','response':'output'}}},ensure_ascii=False,indent=2),encoding='utf8');print({k:sum(s(r)==k for r in train) for k in targets},{k:sum(s(r)==k for r in val2) for k in vt})
