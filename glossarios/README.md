# Glossários

Glossários usados para fixar a tradução de termos recorrentes. Cada arquivo é
um mapeamento `termo original → tradução` (JSON ou YAML simples) que é
inserido no **prompt de sistema** do modelo, na seção `GLOSSÁRIO:`.

## `mushoku_tensei.json` — nomes próprios de *Mushoku Tensei*

Este glossário é de **NOMES PRÓPRIOS** (personagens, lugares, títulos e
raças). Cada termo é mapeado **para si mesmo**, ou seja: a instrução enviada ao
modelo é "**não traduza** este termo".

### Por que ele existe

A regra 2 do prompt ("Preserve NOMES PRÓPRIOS") não é suficiente sozinha.
Na calibração contra o **LM Studio**, o modelo traduziu `Man-God` para
`Deus-Homem` — uma tradução literal que quebra a terminologia do livro.
O glossário reforça a regra de forma explícita e resolve o caso.

### Como usar

Pela **CLI do pacote**:

```bash
python epub_translator.py validacao.epub --glossary glossarios/mushoku_tensei.json
```

Pelo **script de entrega** (`traduzir.py`), que já usa este arquivo por
padrão:

```bash
python traduzir.py                                   # usa o glossário padrão
python traduzir.py --glossary glossarios/mushoku_tensei.json  # caminho explícito
python traduzir.py --no-glossary                     # desliga o glossário
```

Pela **interface web**: use o campo **"Glossário de nomes próprios"** e
envie (upload) este mesmo arquivo `.json` — a interface já aceita JSON.

### Formato

Aceito tanto **JSON** quanto **YAML simples**, com o mesmo formato de
mapeamento `termo → tradução`:

```json
{
  "termo original": "tradução",
  "outro termo": "tradução em pt-BR"
}
```

```yaml
termo original: tradução
outro termo: tradução em pt-BR
```

Um termo com **tradução diferente da original** faz o modelo traduzir
a ocorrência conforme o mapeamento. Exemplo:

```json
{
  "Dragon": "Dragão"
}
```

Com esse mapeamento, `Dragon` vira `Dragão` na tradução. Já o glossário de
nomes próprios usa `termo → termo` (por exemplo `"Dragon": "Dragon"`), e
nesse caso o termo **não** é traduzido.

### Termos do arquivo

43 termos, todos mapeados para si mesmos: `Aisha`, `Ariel`, `Asura`,
`Basara`, `Beast Realm`, `Demon Continent`, `Demon King`, `Dragon`,
`Drake`, `Ehjia`, `Elinalise`, `Eris`, `Fittoa`, `Geese`, `Ghislaine`,
`Giant`, `God of Swords`, `Great Forest`, `Hinata`, `Holy Land`, `Kakuri`,
`Labyrinth`, `Lapis`, `Lilia`, `Man-God`, `Millishion`,
`Millishion Kingdom`, `Mizuha`, `Nanahoshi`, `North`, `Orc`, `Paul`,
`Redo`, `Refugee`, `Roxy`, `Rudeus`, `Sharon`, `Sword God`, `Sword King`,
`Sylphie`, `Talisman`, `Wagon Kingdom`, `Zeil`.
