# Conversa 02: Otimização de Sessão, Prevenção de Bloqueio e Media Browser

- **Data de Ocorrência:** 21/10/2026
- **ID da Conversa:** [`41c3a405-e10c-4b89-ba2c-be407efceeb7`](conversation://41c3a405-e10c-4b89-ba2c-be407efceeb7)
- **Título Original:** Ajustar Automações Intelbras DVR
- **Quantidade de Passos:** 4.036

---

## 1. Problemas Abordados

1. **Tempestade de Logins e Bloqueio de IP no DVR (*Lock Login*):**
   - O DVR Intelbras registrava novos logins a cada requisição HTTP feita pela integração.
   - O excesso de requisições de autenticação concorrentes causava o acionamento do mecanismo de proteção de rede do DVR (*Account Lock* / *Network Lockout*), deixando o dispositivo inacessível na rede local por 30 minutos ou até reinicialização manual.
2. **Erros no `config_flow`:**
   - Falhas no fluxo de configuração e recarregamento da integração no Home Assistant.
3. **Internacionalização (i18n):**
   - Existência de strings estáticas em português espalhadas pelo código sem suporte a `strings.json` e `translations/en.json` / `translations/pt-BR.json`.
4. **Miniaturas e Linha do Tempo no Media Browser e `advanced-camera-card`:**
   - Desejo de exibir previews dinâmicos de cada câmera por data/horário na navegação de mídia sem degradar a reprodução do vídeo.
   - Falhas na criação e inicialização de streams (`create_stream() got an unexpected keyword argument` / `missing dynamic_stream_settings`).

---

## 2. Diagnóstico e Decisões Técnicas

1. **Prevenção do Bloqueio de Rede do DVR:**
   - **Reutilização de Sessão (Keep-Alive / Session Token):** A biblioteca cliente do DVR foi refatorada para manter a sessão HTTP autenticada aberta, reutilizando o cookie/token em vez de realizar login/logout a cada leitura de sensor.
   - **Stream de Eventos Push:** Substituição do polling periódico agressivo por conexão contínua de escuta de eventos via HTTP multipart (`eventManager.cgi`).
2. **Sensores de Diagnóstico:**
   - Criação de sensores diagnósticos para monitorar carga de CPU, uso de memória e erros de autenticação do DVR, mantidos ocultos por padrão (`entity_registry_enabled_default = False`) com traduções completas.
3. **Media Browser & Concorrência de Previews:**
   - Descoberta de gargalo: a geração concorrente de thumbnails sobrecarregava as threads de vídeo do DVR, impedindo a reprodução suave do arquivo selecionado.
   - **Priorização de Reprodução:** Implementação de fila com prioridade para streams ativos, pausando requisições de miniaturas secundárias enquanto um vídeo estiver sendo reproduzido.
   - Ajuste das assinaturas da API de streaming do Home Assistant Core (`create_stream`, `dynamic_stream_settings`).
   - Suporte e compatibilidade aperfeiçoados com o card Lovelace `custom:advanced-camera-card`.
