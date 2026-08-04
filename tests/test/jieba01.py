import jieba


def jieba_cut(text):
    # 分词
    result = jieba.cut(text)
    # <generator object>大型文本可能：几百万字。如果直接：一次性加载全部结果 浪费内存。流式生成
    print(f"精准模式：{type(result)}")
    print(" / ".join(result))

def jieba_lcut(text):
    # 分词
    result = jieba.lcut(text)
    # <list>
    print(f"精准模式：{type(result)}")
    print(" / ".join(result))

def jieba_lcut_all(text):
    # 分词
    result = jieba.lcut(text,cut_all=True)
    # <list>
    print(f"全模式：{type(result)}")
    print(" / ".join(result))

def jieba_cut_for_search(text):
    # 分词
    result = jieba.cut_for_search(text)
    # <generator object>
    print(f"搜索引擎模式：{type(result)}")
    print(" / ".join(result))
if __name__ == '__main__':
    text = "南京市长江大桥"
    jieba_cut(text)
    jieba_lcut_all(text)
    jieba_cut_for_search(text)