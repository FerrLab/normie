# Normie

Normalizador de voz para o Discord: equaliza **o que você ouve** numa call. Quem fala
alto demais é abaixado, quem fala baixinho é aumentado — automaticamente, enquanto a
call acontece.

- Só afeta **você**. O Normie mexe no mesmo controle de
  *clique direito no amigo → Volume do usuário* do seu Discord.
- Seus amigos não instalam nada e não percebem nada.
- Ao fechar o app, o próprio Discord devolve os volumes que estavam antes.

## Instalar

**Opção 1 — `.exe` (sem Python):** baixe o `normie.exe` da
[última release](https://github.com/FerrLab/normie/releases/latest) e abra.
O Windows SmartScreen pode avisar que o app não é assinado → *Mais informações* →
*Executar assim mesmo*.

**Opção 2 — Python 3.10+:** baixe o `normie.py` da release (ou clone o repo) e rode:

```powershell
pip install -r requirements.txt
```

```powershell
python discord_normalizer.py
```

## Como usar

1. Clique em **Conectar**.
2. Na primeira vez, o Discord mostra um pedido de autorização → **Autorizar**.
   Depois disso o app reconecta sozinho sempre que abrir.
3. Entre numa call. Cada pessoa aparece num cartão:
   - **barra**: o quão alto você está ouvindo a pessoa, com a marca branca no alvo
     (verde = no alvo, amarelo = baixo, vermelho = alto);
   - **"microfone chega a …"**: o nível dela antes do seu volume;
   - **volume** atual no seu Discord, com ▲/▼ logo após um ajuste;
   - **AUTO / TRAVADO**: clique para impedir ajustes naquela pessoa (ex.: bot de música).
4. **Bandeja:** minimizar esconde a janela no ícone perto do relógio, e o Normie continua
   trabalhando. Clique no ícone para abrir; clique com o botão direito para
   ligar/desligar a normalização ou sair. Fechar a janela (X) encerra o app.

### Ajustes

| Controle | O que faz |
|---|---|
| Normalização automática | Liga/desliga os ajustes (a medição continua). |
| Nível alvo | Para onde todo mundo é puxado. Ajuste de ouvido. |
| Ajustes avançados → Velocidade | Quanto do erro é corrigido a cada ~2 s de fala. |
| Ajustes avançados → Volume permitido | Limites do slider (o Discord aceita 0–200%). |
| Ajustes avançados → Saída monitorada | Dispositivo onde o Discord toca (fone/caixa). |
| Ajustes avançados → Minimizar esconde na bandeja | Desligue para minimizar normalmente. |
| Atividade | Histórico de ajustes e erros. |

## Como funciona

```
 sua placa de som ──loopback──►  mede o nível (dBFS) a cada 100 ms
                                        │
 Discord (RPC local) ──SPEAKING_START──► quem está falando agora?
                                        │  só 1 amigo falando → o nível é dele
                                        ▼
                         a cada ~2 s de fala dessa pessoa:
                         compute_new_volume() decide o novo volume
                                        │
 Discord ◄──SET_USER_VOICE_SETTINGS─────┘  (slider de volume daquele amigo)
```

O login usa o app do Discord cujo ID está em `CLIENT_ID`, no topo do script. Ele é um
**Public Client**, então a autorização usa PKCE: cada login gera um código aleatório de
uso único no lugar de uma senha do app, e não existe Client Secret.

## Usando em outra conta ou com outro app

- **Outra conta do Discord:** o Discord só libera RPC de apps não aprovados para o dono e
  para contas autorizadas no app. Se um amigo for usar o script com este `CLIENT_ID`, a
  conta dele precisa ser adicionada ao app no Developer Portal; senão a autorização falha.
- **Seu próprio app:** crie um em <https://discord.com/developers/applications>. Em
  **OAuth2**, ligue **Public Client**, adicione o redirect `http://localhost` e troque o
  `CLIENT_ID` no topo do script.

## Limitações

- **Outros sons na mesma saída atrapalham a medição.** O app mede tudo que toca no
  dispositivo escolhido. Jogo ou música alta junto da fala distorcem a leitura. Se puder,
  ponha o Discord numa saída e o jogo em outra (Discord → Configurações → Voz e Vídeo →
  Dispositivo de saída) e escolha a do Discord em *Saída monitorada*.
- **Só mede quando uma pessoa fala sozinha.** Com fala sobreposta, nada é medido e os
  volumes ficam onde estão.
- **200% é o teto do Discord (~+6 dB).** Quem tem microfone muito baixo pode não
  alcançar o alvo.
- **Um app de cada vez.** O Discord só deixa um programa controlar voice settings por RPC
  de cada vez. Plugins de Stream Deck/Touch Portal para o Discord podem conflitar.
- Windows apenas (usa WASAPI e o pipe IPC do Discord desktop).

## Arquivo de configuração

`discord_normalizer_config.json` (criado ao lado do script ou do `.exe`) guarda os
ajustes e o **token de acesso** em texto puro. Não compartilhe esse arquivo; ele já está
no `.gitignore`.

## Publicar uma versão

Em **Actions → Release → Run workflow**, informe a versão (ex.: `1.0.0`). O workflow, num
runner Windows:

1. valida a versão e confere que a tag `v1.0.0` ainda não existe;
2. grava a versão no script (`APP_VERSION`) e confere que ele importa;
3. gera o ícone e o `normie.exe` com PyInstaller (`--onefile --windowed`);
4. cria a release `v1.0.0` com `normie.exe`, `normie.py`, `requirements.txt` e
   `SHA256SUMS.txt`, com notas geradas a partir dos commits.
