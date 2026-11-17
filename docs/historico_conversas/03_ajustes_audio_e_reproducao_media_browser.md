# Conversa 03: Ajustes de Áudio e Reprodução no Media Browser

- **Data de Ocorrência:** 23/10/2026
- **ID da Conversa:** [`050f20f3-ff12-44f7-8744-078e3891c361`](conversation://050f20f3-ff12-44f7-8744-078e3891c361)
- **Título Original:** Ajuste do Media Browser Intelbras
- **Quantidade de Passos:** 4.332

---

## 1. Problemas Abordados

1. **Ausência de Áudio nas Gravações das Câmeras com Microfone:**
   - Câmeras com captação de áudio (Quintal, Fundo Casa do Biso, Portão Social e Vídeo Porteiro) tinham vídeo reproduzido normalmente, porém sem áudio nas gravações gravadas no DVR.
2. **Concorrência entre Geração de Miniaturas e Reprodução:**
   - Durante a navegação em diretórios com múltiplos vídeos, a tentativa simultânea de extrair frames de preview travava a abertura do fluxo de gravação.
3. **Detecção e Filtragem Dinâmica de Câmeras Inválidas (Canal 12):**
   - O DVR expunha canais vazios ou desconectados (como a câmera 12) que eram listados no `config_flow`, poluindo as entidades.
4. **Privacidade e Sanitização de Documentação:**
   - Necessidade de remover números de série, credenciais e endereços IP reais do repositório antes de publicar.

---

## 2. Diagnóstico e Decisões Técnicas

1. **Suporte a Áudio em Mídia Gravada:**
   - Identificou-se que a montagem da URL RTSP/HTTP de playback no DVR necessitava do parâmetro explícito de faixa de áudio (`subtype=0` e flags de áudio nos canais de streaming).
   - Configuração dinâmica de propriedades de reprodução de áudio por canal (detecção automática de suporte a áudio no canal do DVR).
2. **Cache de Módulos Python no Home Assistant:**
   - Diagnóstico de que `reload_config_entry` não recarregava classes Python já importadas em `sys.modules`, exigindo o reinício do processo do Home Assistant para que mudanças na camada de `media_source` entrassem em efeito.
3. **Filtragem de Canais no `config_flow`:**
   - Implementação de verificação de status ativo (`GetChannelTitle` / `videoInput`) durante o `config_flow` e `options_flow`, ocultando canais inativos ou fantasmas como o canal 12.
4. **Sanitização de Documentação:**
   - Varredura geral no `README.md` e `README.pt-BR.md` para remover serial numbers, nomes de rede privados e substituição por placeholders genéricos (`192.168.0.X`, `dvr_intelbras`).
