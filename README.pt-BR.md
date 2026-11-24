# Integração Intelbras DVR para Home Assistant

<p align="center">
  <img src="https://raw.githubusercontent.com/fabiobrolo/intelbras_dvr/main/custom_components/intelbras_dvr/brand/logo.png" alt="Intelbras DVR" width="300"/>
</p>

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/default)
[![Home Assistant](https://img.shields.io/badge/home--assistant-2024.4%2B-brightgreen.svg)](https://www.home-assistant.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[🇺🇸 English](README.md) | [🇧🇷 Português](README.pt-BR.md)

> ⚠️ **Aviso Legal**: Intelbras® e Dahua® são marcas registradas de seus respectivos proprietários. Esta é uma integração comunitária não oficial sem qualquer afiliação, endosso ou suporte da Intelbras S.A. ou Dahua Technology.
>
> 📌 **Escopo (Foco em DVRs e NVRs):** Esta integração foi projetada como um **hub** para **DVRs e NVRs Intelbras/Dahua** (dispositivo gravador principal + dispositivos filhos por canal de câmera). Ela **não** é voltada para câmeras IP unitárias/avulsas — se suas câmeras IP estão conectadas ao DVR/NVR, gerencie-as pela própria entrada do DVR/NVR; para câmeras IP avulsas sem gravador, utilize uma integração dedicada de câmera IP (como ONVIF ou Generic Camera).

---

Integração personalizada de alta performance para o **Home Assistant** dedicada a DVRs e NVRs **Intelbras** (e equipamentos compatíveis baseados em Dahua) em conformidade com a especificação **API of HTTP Protocol Specification V3.35**. Comunicação 100% local através de APIs HTTP CGI (stream de eventos push `local_push` + `DataUpdateCoordinator`) e fluxos de vídeo RTSP, com zero dependência de nuvem.

Desenvolvida seguindo os padrões arquiteturais modernos do Home Assistant (HA 2024.4+ / 2026.x / Python 3.14), utilizando `ConfigEntry[IntelbrasDVRData]` tipado (`runtime_data`), dataclasses `EntityDescription` em todas as plataformas, descoberta 100% dinâmica de hardware/canais/mosaicos de vídeo, fluxos nativos de Reautenticação (`reauth`) e Reconfiguração (`reconfigure`), localização completa em Português do Brasil e Inglês, execução assíncrona não bloqueante no event loop, transcodificação dinâmica de áudio e reprodução de gravações diretamente no Navegador de Mídia com miniaturas de pré-visualização.

---

## 🌟 Recursos e Capacidades Principais

### 📹 Streaming de Vídeo e Motor de Reprodução Adaptativo
* **Fluxos RTSP Duplos:** Stream Principal de alta resolução (`subtype=0`) e Stream Secundário de baixo consumo de banda (`subtype=1`) por canal.
* **Instantâneos Inteligentes:** Entidades de câmera do tipo snapshot com intervalo configurável, cache local e captura ultra-rápida via CGI/FFmpeg.
* **Áudio Híbrido Adaptativo (go2rtc):** Canais que possuem áudio (ex.: videoporteiros, câmeras com microfone embutido ou áudio coaxial HDCVI) registram-se dinamicamente sob demanda no `go2rtc` utilizando `#video=copy#audio=aac#async`, garantindo reprodução de áudio fluida e sem engasgos em navegadores modernos e aplicativos móveis.
* **Reprodução de Gravações no Navegador de Mídia:** Navegue e assista a gravações passadas do DVR organizadas por data e câmera diretamente no Home Assistant, com linha do tempo ribbon e miniaturas geradas sob demanda.

### 🎨 Perfis de Imagem, Compensação de Luz e Agendamento de Períodos
* **Seletor Dinâmico de Perfil / Período de Configuração:**
  * **Câmeras Analógicas (`select.camera_*_period_config`):** Alternância entre `Vigente (Tempo real)`, `Período 1` e `Período 2`.
  * **Câmeras IP Intelbras (`select.camera_*_profile_config`):** Alternância entre `Vigente (Tempo real)`, `Dia`, `Noite` e `Regular`.
* **Exibição em Tempo Real e Edição Isolada:**
  * No modo padrão **Vigente (Tempo real)**, todos os controles deslizantes, seletores e chaves refletem exatamente o estado do hardware em tempo real de acordo com o agendamento em vigor.
  * Ao selecionar um período ou perfil específico, todos os controles atualizam instantaneamente para exibir os parâmetros salvos daquele período. Quaisquer alterações realizadas são gravadas diretamente no índice correspondente no DVR (`VideoColor[ch][prof]`, `VideoInDenoise[ch][prof]`, `ImageEnhancement[ch][prof]`, `NormalOptions`, `NightOptions`) sem alterar os demais períodos.
* **Modo de Compensação de Luz (`select.camera_*_light_compensation_mode`):** Seletor unificado e mutuamente exclusivo suportando `Desabilitado`, `BLC` (Compensação de Luz de Fundo), `HLC` (Compensação de Luz Alta) e `WDR` (Amplo Alcance Dinâmico), populado dinamicamente com base nas capacidades de hardware de cada câmera (`getCaps`).
* **Modos Dia/Noite e Estilo de Imagem:**
  * Alternância entre `Colorido`, `Auto` e `Preto e Branco` (`select.camera_*_day_night_mode`) por perfil.
  * Seleção do regime de gerenciamento de perfil: `Regular`, `Perfil Fixo`, `Agendamento` e `Dia/Noite` (`select.camera_*_profile_mode`).
  * Estilos de imagem / modos de cena: `Padrão`, `Suave` e `Vivo` (`select.camera_*_scene_mode`).
* **Redução de Ruído e Ganho de Imagem:**
  * Controle deslizante de Redução de Ruído 2D (`number.camera_*_noise_reduction`) e chave de Redução Automática de Ruído (`switch.camera_*_auto_noise_reduction`).
  * Controle deslizante de Ganho de Imagem (`number.camera_*_image_gain`) e Ajuste de Vídeo / nitidez (`number.camera_*_acutance`).
* **Agendamento Contínuo e Sem Conflitos (Câmeras Analógicas):** Coordenação robusta de ciclo de 24 horas que calcula automaticamente intervalos complementares entre o Período 1 e o Período 2, prevenindo sobreposições ou conflitos de horário ao ativar ou alterar períodos.

### ✍️ Texto Dinâmico Sobreposto no Vídeo (OSD)
* **Texto Gravado na Imagem (`text.camera_*_channel_title_text`):** Entidade de texto gravada em tempo real diretamente no fluxo de vídeo ao vivo e nas gravações através do `ChannelTitle.Name` (até 64 caracteres). Atualize temperaturas externas, status de alarmes, sensores de portão ou avisos climáticos dinamicamente através de automações.
* **Visibilidade de Itens OSD:** Chaves independentes para exibir ou ocultar o Nome do Canal (`switch.camera_*_channel_title_overlay`) e a Data/Hora (`switch.camera_*_time_title_overlay`).

### 🕹️ Controles PTZ e Telemetria
* **Movimentação Direcional:** Botões para Cima, Baixo, Esquerda, Direita e Zoom (+ / -) com velocidade ajustável e parada automática temporizada para prevenir movimentos descontrolados.
* **Navegação por Predefinições:** Descoberta e posicionamento automático em presets salvos no hardware via `select.camera_*_ptz_preset`.
* **Coordenadas de Hardware:** Atributos de estado expõem coordenadas em tempo real de `pan`, `tilt`, `zoom` e `move_status`.

### 🚨 Eventos Push em Tempo Real e IA Perimetral IVS
* **Fluxo HTTP de Baixa Latência (`eventManager.cgi?action=attach`):** Conexão push persistente fornecendo alertas instantâneos sem necessidade de logins recorrentes:
  * Detecção de Movimento (`binary_sensor.camera_*_motion`).
  * Perda de Vídeo / desconexão de câmera (`binary_sensor.camera_*_video_loss`).
  * Mascaramento / adulteração de lente (`binary_sensor.camera_*_tampering`).
  * IVS Linha Virtual / Tripwire (`binary_sensor.camera_*_linha_virtual`).
  * IVS Intrusão Perimetral / Cerca Virtual (`binary_sensor.camera_*_cerca_virtual`).
  * Alertas de Falha de Disco, Disco Ausente e Espaço em Disco Crítico.
  * Alarme de Tentativa de Invasão por Força Bruta (`binary_sensor.dvr_*_login_failure`).

### 📊 Saúde do Sistema, Diagnósticos e Diagnostics.py
* **Monitoramento de Usuários Conectados (`sensor.dvr_*_active_users`):** Acompanhamento em tempo real de sessões ativas, clientes (`Web3.0`, `Dahua3.0`, aplicativos móveis), endereços IP e horários de login.
* **Diagnóstico de Armazenamento e HD:** Detecção de falhas de hardware/S.A.R.T. (`binary_sensor.dvr_*_storage_failure`), atributos com tabela de saúde das partições, capacidade total/livre e botão de atualização sob demanda (`button.dvr_*_refresh_storage`).
* **Telemetria de Rede:** Velocidade do link (Mbit/s), endereço MAC e sensor binário de integridade do cabo de rede (`binary_sensor.dvr_*_network_link`).
* **Diagnóstico Oficial do Home Assistant:** Suporte nativo a `diagnostics.py` permitindo baixar o relatório JSON com um clique em **Configurações > Dispositivos e Serviços > Intelbras DVR > menu de 3 pontos** (ou no dispositivo da câmera), com ofuscação automática de senhas e dados sensíveis.
* **Serviço de Consulta de Logs:** Serviço customizado `intelbras_dvr.get_logs` para consultar registros estruturados de auditoria e eventos diretamente no Home Assistant.

### 🖥️ Gerenciamento da Saída Física de Monitor (HDMI/VGA)
* **Seleção Dinâmica de Mosaico (Split):** Consulta em tempo real via `encode.cgi?action=getCaps` (`PreviewSplitNumList`) e `split.cgi` para gerar opções de mosaico (`1x1`, `2x2`, `1+7`, `3x3`, `4x4`, `5x5`, `6x6`) correspondentes ao hardware conectado.
* **Prevenção de AutoLogout:** Mantém a saída de vídeo HDMI/VGA sempre ligada 24/7 desativando o tempo limite de ociosidade (`AutoLogout=0`).

### 🛡️ Resiliência Corporativa e Estabilidade de Autenticação
* **Fila Serializada de Autenticação Digest:** Semáforo assíncrono estrito (`_request_lock`) que elimina condições de corrida, limpa cookies CGI expirados e gerencia a renegociação de nonces Digest automaticamente.
* **Proteção Anti-Bloqueio de Usuário:** Substitui falhas abruptas de autenticação por alertas não-bloqueantes em **Reparos** e um recuo progressivo (*backoff cooldown*), impedindo o bloqueio temporário do usuário no DVR.
* **Tolerância a Falhas Transitórias:** Preserva o estado anterior das entidades durante oscilações momentâneas de rede sem disparar falsos alertas de indisponibilidade.

---

## 📊 Compatibilidade de Hardware

Modelos e capacidades verificados:

| Modelo / Firmware | RTSP ao Vivo | Snapshots | Gravações | Áudio (go2rtc) | Eventos Push | Controles de Imagem | OSD Sobreposto | Controles PTZ | Mosaico HDMI/VGA | Diagnósticos do Sistema |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Intelbras iMHDX 3008**<br>`4.001.00IB001.0.T` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

*Compatível com gravadores Intelbras das linhas MHDX, iMHDX, NVD e equipamentos OEM baseados em Dahua executando a API HTTP V3.35.*

---

## 🌟 Entidades e Plataformas

### 🖥️ Entidades do Dispositivo DVR
| Plataforma | Chave da Entidade | Categoria | Descrição |
| :--- | :--- | :--- | :--- |
| `sensor` | `dvr_status` | Diagnóstico | Estado da conexão (`online` / `offline`), versão do firmware, número de série |
| `sensor` | `monitor_mode` | Diagnóstico | Modo de divisão HDMI/VGA ativo com metadados de visualização e grupo |
| `sensor` | `disk_usage` | Diagnóstico | Capacidade do disco rígido (`total_gb`, `free_gb`) e porcentagem de uso |
| `sensor` | `active_users` | Diagnóstico | Contagem e detalhes de sessão dos clientes conectados atualmente |
| `sensor` | `network_speed` | Diagnóstico | Velocidade do link da interface ethernet (Mbit/s) |
| `binary_sensor` | `event_stream` | Diagnóstico | Conectividade do fluxo de eventos HTTP em tempo real |
| `binary_sensor` | `storage_failure` | Diagnóstico | Alerta de falha de hardware / erro de I/O no disco |
| `binary_sensor` | `storage_not_exist` | Diagnóstico | Alerta de disco de armazenamento ausente ou desconectado |
| `binary_sensor` | `storage_low_space` | Diagnóstico | Alerta de pouco espaço livre no disco |
| `binary_sensor` | `login_failure` | Diagnóstico | Alerta de tentativas repetidas de invasão ou login incorreto |
| `binary_sensor` | `network_link` | Diagnóstico | Conectividade física do cabo de rede ethernet |
| `select` | `monitor_select` | Configuração | Seletor de modo de mosaico para monitor físico HDMI/VGA |
| `button` | `dvr_reboot` | Configuração | Botão para reinicializar o hardware do DVR |
| `button` | `dvr_refresh` | Diagnóstico | Atualização dos dados do coordenador sob demanda |
| `button` | `refresh_storage` | Diagnóstico | Atualização da telemetria de integridade das partições do HD |
| `update` | `firmware_update` | Configuração | Verificador de atualizações de firmware e acionador de upgrade em nuvem |

### 📹 Entidades dos Dispositivos de Câmera (Modulares via Config & Options Flow)
| Plataforma | Chave da Entidade | Categoria | Descrição |
| :--- | :--- | :--- | :--- |
| `camera` | `main_stream` | Nenhuma | Fluxo RTSP de alta resolução (`subtype=0`) |
| `camera` | `sub_stream` | Nenhuma | Fluxo RTSP de baixa resolução (`subtype=1`) |
| `camera` | `snapshot` | Nenhuma | Entidade de imagem estática com intervalo de atualização configurável |
| `binary_sensor` | `motion` | Nenhuma | Detecção de movimento em tempo real |
| `binary_sensor` | `video_loss` | Diagnóstico | Desconexão de câmera / perda de sinal de vídeo |
| `binary_sensor` | `video_blind` | Diagnóstico | Mascaramento / adulteração da lente da câmera |
| `binary_sensor` | `cross_line` | Nenhuma | Detecção de Linha Virtual (IVS Tripwire) |
| `binary_sensor` | `cross_region` | Nenhuma | Detecção de Intrusão Perimetral / Cerca Virtual (IVS) |
| `select` | `period_config` | Configuração | Seletor de período (`active`, `period_1`, `period_2`) para visualização/edição (Analógicas) |
| `select` | `profile_config` | Configuração | Seletor de perfil (`active`, `day`, `night`, `regular`) para visualização/edição (IP) |
| `select` | `light_compensation_mode` | Configuração | Compensação de luz (`disabled`, `blc`, `hlc`, `wdr`) por perfil |
| `select` | `day_night_mode` | Configuração | Modo dia/noite (`black_white`, `color`, `schedule`) por perfil (IP) |
| `select` | `profile_mode` | Configuração | Regime de perfil (`regular`, `fixed`, `schedule`, `day_night`) (IP) |
| `select` | `fixed_profile` | Configuração | Perfil fixo selecionado (`day`, `night`) (IP) |
| `select` | `scene_mode` | Configuração | Estilo de imagem (`standard`, `soft`, `vivid`) (IP) |
| `select` | `record_mode` | Configuração | Modo de agendamento de gravação (`schedule`, `manual`, `off`) |
| `select` | `ptz_preset` | Configuração | Movimentação da câmera para um preset PTZ descoberto |
| `number` | `brightness`, `contrast`, `hue`, `saturation` | Configuração | Controles deslizantes de cor e imagem (`0`–`100`) por perfil |
| `number` | `acutance` | Configuração | Ajuste de vídeo / nitidez (`0`–`15`) por perfil |
| `number` | `noise_reduction` | Configuração | Controle deslizante de Redução de Ruído 2D (`0`–`100`%) por perfil |
| `number` | `image_gain` | Configuração | Controle deslizante de Ganho de Imagem (`0`–`100`%) por perfil |
| `number` | `motion_sensitivity`, `motion_threshold` | Configuração | Controles deslizantes de Sensibilidade e Limiar de detecção de movimento (`0`–`100`) por canal |
| `switch` | `motion_detection_enable` | Configuração | Ativar ou desativar a detecção de movimento do DVR por canal (expõe sensibilidade, limiar, luz quente, bipe e gravação) |
| `switch` | `auto_noise_reduction` | Configuração | Chave de Redução Automática de Ruído (`GainAuto`) por perfil |
| `switch` | `privacy_mask` | Nenhuma | Mascaramento (escurecimento total) ou restauração da imagem de vídeo |
| `switch` | `channel_title_overlay` | Configuração | Exibir ou ocultar a sobreposição do nome do canal no vídeo |
| `switch` | `time_title_overlay` | Configuração | Exibir ou ocultar a sobreposição de data/hora no vídeo |
| `switch` | `period_1_active`, `period_2_active` | Configuração | Ativar ou desativar períodos de agendamento analógico |
| `text` | `channel_title_text` | Configuração | Texto dinâmico OSD gravado diretamente no fluxo de vídeo (até 64 caracteres) |
| `time` | `day_start_time`, `day_end_time` | Configuração | Horários de início e término do dia para agendamento (IP) |
| `time` | `period_1_start_time`, `period_1_end_time` | Configuração | Horários do Período 1 (Analógicas) |
| `time` | `period_2_start_time`, `period_2_end_time` | Configuração | Horários do Período 2 (Analógicas) |
| `button` | `camera_reset_defaults` | Configuração | Restaurar configurações padrão de imagem para o perfil ativo ou selecionado |
| `button` | `ptz_up`, `ptz_down`, `ptz_left`, etc. | Nenhuma | Botões de movimentação direcional e zoom PTZ |

---

### 💡 Exemplo de Automação com Texto Dinâmico no Vídeo (OSD)

```yaml
automation:
  - alias: "Atualizar OSD da câmera com a temperatura externa"
    trigger:
      - platform: state
        entity_id: sensor.temperatura_externa
    action:
      - action: text.set_value
        target:
          entity_id: text.camera_canal_1_titulo_da_camera
        data:
          value: "Temp: {{ states('sensor.temperatura_externa') }}°C"
```

---

## ⏱️ Estratégia de Polling e Intervalos de Atualização

Para proteger o servidor HTTP/CGI embarcado do DVR contra sobrecarga (especialmente durante a reconexão de câmeras IP após reinicializações), a integração utiliza uma **arquitetura de atualização em camadas**, com requisições serializadas e um **intervalo mínimo de 150 ms (`pacing`)** entre chamadas CGI sequenciais:

| Camada de Atualização | Intervalo | Dados e Endpoints Consultados | Atualização Imediata sob Demanda |
|---|---|---|---|
| **Push em Tempo Real** | Contínuo (`heartbeat=5s`) | Stream HTTP persistente (`eventManager.cgi?action=attach`): Movimento, Smart Motion (Humano/Veículo), IVS (Linha/Cerca Virtual), Perda de Vídeo, Mascaramento, Alertas de HD e Falha de Login | Instantâneo (sem polling) |
| **Polling Rápido (Local no DVR)** | A cada **60 s** | Apenas tabelas leves locais do DVR (`VideoColor`, `RecordMode` — ~13 KB, sem o peso do `Encode` e sem RPC síncrono para câmeras IP remotas), Modo do Monitor (`split.cgi`), Usuários Conectados (`userManager.cgi`) e Hora do DVR (`global.cgi`) | Ao alterar saída de monitor, modo de gravação ou ajustes básicos de imagem |
| **Polling Lento (Estendido e Diagnóstico)** | A cada **5 min (300 s)** | Configurações estendidas de câmera (`Encode`, `VideoWidget`, `VideoInOptions`, `VideoInDenoise`, `ImageEnhancement` — com fallback automático para tabelas locais caso algum RPC de câmera IP dê timeout), Detecção de Movimento por Canal (`MotionDetect`), Diagnóstico e Uso de HD (`storageDevice.cgi`, sem chamadas duplicadas, com atributos `healthy`, `total_human` e `preallocated_disk`), Interface e Velocidade de Rede (`netApp.cgi`) e Status PTZ (`ptz.cgi`) | Ao pressionar **Atualizar dados** / **Atualizar câmera** / **Atualizar diagnóstico do HD**, alternar detecção de movimento, ou editar qualquer entidade de perfil, OSD, compensação de luz ou redução de ruído no HA |
| **Verificação de Firmware** | A cada **6 horas** | Verificação de nova versão de firmware (`CloudUpgrader/check` / `upgrade.cgi`) | Na inicialização da integração |
| **Cache Único (Inicialização)** | Startup / Reconfiguração | Dados e versão de hardware (`magicBox.cgi?action=getSystemInfo`, `getSoftwareVersion`, `getHardwareVersion`), Títulos dos canais e Dispositivos IP remotos, Capacidades de hardware (`encode.cgi?action=getCaps`, `devVideoInput.cgi?action=getCaps`), Predefinições PTZ e Auditoria de permissões do usuário (`userManager.cgi?action=getUserInfo`) | Ao recarregar ou reconfigurar a integração |
| **Instantâneos (Snapshots)** | Configurável (`0,5s`–`30s`, padrão `1,0s`) | `snapshot.cgi` (requisitado apenas enquanto estiver sendo visualizado ativamente em um dashboard) | Dinâmico via `number.*_taxa_de_atualizacao_dos_instantaneos` |

---

## 🔐 Pré-requisitos: Usuário Dedicado no DVR e Permissões (Recomendado)

Antes de adicionar a integração, é **fortemente recomendado** criar uma conta de usuário dedicada especificamente para o Home Assistant no seu DVR Intelbras (**Sistema > Conta > Usuário**):

1. **Por que utilizar um usuário dedicado?**
   * Previne conflitos de sessão e bloqueios temporários quando contas pessoais (`admin`) são acessadas simultaneamente por aplicativos de celular (Intelbras iSIC / Guardian) ou navegadores web.
   * Mantém os logs de auditoria do DVR limpos e organizados.
   * Ative a opção **Reutilizável** na conta de usuário, caso esteja disponível em seu firmware.

2. **Permissões Necessárias:**
   Associe o usuário ao grupo **`admin`** (ou a um grupo personalizado com as seguintes permissões habilitadas):
   * **Visualização ao Vivo / Monitor** & **Reprodução / Pesquisa**: Fluxos RTSP ao vivo, snapshots e reprodução no Navegador de Mídia.
   * **Controle PTZ**: Movimentação de câmeras e chamada de predefinições.
   * **Configurações de Sistema / Encode / Câmera / Gravação / Exibição**: Controles de imagem, sobreposição OSD, máscara de privacidade, modos de gravação e layout do monitor HDMI/VGA.
   * **Informações do Sistema / Armazenamento / Manutenção**: Diagnósticos de integridade do disco e reinicialização do equipamento.

---

## 📦 Instalação

### Via HACS (Recomendado)
1. Certifique-se de ter o [HACS](https://hacs.xyz/) instalado.
2. Abra o **HACS** no Home Assistant.
3. Clique nos três pontos no canto superior direito e escolha **Repositórios personalizados**.
4. Insira a URL: `https://github.com/fabiobrolo/intelbras_dvr`
5. Selecione a categoria: **Integração**.
6. Clique em **Adicionar**, localize **Intelbras DVR** na listagem e clique em **Baixar**.
7. Reinicie o Home Assistant.

### Instalação Manual
1. Copie o diretório `custom_components/intelbras_dvr/` para a sua pasta `/config/custom_components/`.
2. Reinicie o Home Assistant.

---

## ⚙️ Configuração, Opções e Manutenção

1. No Home Assistant, acesse **Configurações > Dispositivos e Serviços > Adicionar Integração**.
2. Pesquise por **Intelbras DVR**.
3. **Passo 1 - Conexão:** O fluxo de configuração faz uma busca automática na rede local via **DHDiscover (UDP 37810)** para listar **DVRs e NVRs** Intelbras/Dahua ainda não configurados, sem exigir credenciais (câmeras IP unitárias e gravadores já cadastrados são filtrados automaticamente). Selecione um DVR/NVR descoberto na lista ou digite qualquer host/IP manualmente, e informe usuário, senha, porta HTTP (padrão `80`) e porta RTSP (padrão `554`).
4. **Passo 2 - Canais de Câmera:** A integração descobre automaticamente todos os canais ativos, seus nomes e suporte a PTZ. Selecione as câmeras que deseja importar.
5. **Passo 3 - Seleção de Recursos:** Habilite os conjuntos de recursos desejados (`main_stream`, `sub_stream`, `snapshot`, `motion`, `video_color`, `profiles`, `overlay`, `ptz`, `recording`, etc.) e as opções de monitoramento de sistema.

> 💡 **Opções (Options Flow):** Clique em **Configurar** no cartão da integração a qualquer momento para adicionar ou remover canais de câmera e recursos sem recriar entidades.
>
> 🔧 **Reconfiguração (Reconfigure Flow):** Utilize **Reconfigurar** no menu do cartão para atualizar host, portas ou credenciais sem perder o histórico das entidades.

---

## 🛠️ Serviços

| Serviço | Descrição | Parâmetros |
|---|---|---|
| `intelbras_dvr.set_video_color` | Ajusta propriedades de imagem em um canal de câmera | `channel` (`1`–`32`), `brightness`, `contrast`, `hue`, `saturation`, `acutance` (`0`–`100`), `profile` (`0`=Dia, `1`=Noite, `2`=Normal) |
| `intelbras_dvr.mask_camera` | Aplica ou remove máscara de privacidade (escurecimento total) | `channel` (`1`–`32`), `action` (`mask` / `unmask`) |
| `intelbras_dvr.set_monitor_split` | Altera o modo de divisão do monitor físico HDMI/VGA | `mode` (`split1`, `split4`, `split8`, `split9`, `split16`, etc.), `group`, `channel` |
| `intelbras_dvr.record_camera` | Altera a câmera para gravação manual por um período determinado | `channel` (`1`–`32`), `duration` (`5`–`3600` segundos) |
| `intelbras_dvr.ptz_move` | Move uma câmera PTZ em uma direção com parada automática | `channel` (`1`–`32`), `direction`, `speed` (`1`–`8`), `duration` (`0.1`–`10.0`s) |
| `intelbras_dvr.ptz_stop` | Interrompe o movimento PTZ em andamento | `channel` (`1`–`32`), `direction` |
| `intelbras_dvr.ptz_preset` | Move a câmera PTZ para uma predefinição salva no hardware | `channel` (`1`–`32`), `preset` (`1`–`255`) |
| `intelbras_dvr.get_logs` | Consulta registros estruturados de auditoria e eventos do sistema (incluindo `detail_fields` estruturado) | `count` (`1`–`100`), `log_type`, `start_time`, `end_time` |
| `intelbras_dvr.get_config` | Lê qualquer tabela `configManager` do DVR pelo nome (retorna dados de resposta) | `name` (ex.: `MotionDetect`, `Encode`, `NTP`) |
| `intelbras_dvr.set_config` | Grava diretamente um ou mais parâmetros `configManager` no DVR | `params` (mapa chave/valor; strings vazias limpam campos automaticamente) |
| `intelbras_dvr.reboot` | Reinicializa o hardware do DVR | `entry_id` (opcional) |

> 💾 **Nota sobre Uso de Disco (`100%`):** DVRs Intelbras/Dahua pré-alocam 100% do espaço do HD na formatação para gravação circular. Exibir `100,0%` em `sensor.*_uso_de_disco` é normal — consulte os atributos `healthy` e `preallocated_disk` nos sensores de Status do HD e Uso de Disco para verificar a saúde real do disco.

---

## 📺 Linha do Tempo e Miniaturas com `custom:advanced-camera-card`

```yaml
type: custom:advanced-camera-card
cameras:
  - camera_entity: camera.minha_camera_dvr
    live_provider: ha # ou go2rtc
    media:
      type: folder
      folders:
        - gravacoes_dvr

folders:
  - id: gravacoes_dvr
    type: ha
    ha:
      url: /media-browser/browser/app%2Cmedia-source%3A%2F%2Fintelbras_dvr%2FALLFILES%7C<ENTRY_ID>%7C<CH>%7C<YYYYMMDD>
      path:
        - matchers:
            - type: title
          parsers:
            - type: date
              regexp: '(?<value>\d{2}:\d{2})'
              format: 'HH:mm'

timeline:
  show_recordings: true
  style: ribbon
  window_seconds: 3600
```

---

## 👨‍💻 Autor e Mantenedor

* **Fábio Brolo** ([@fabiobrolo](https://github.com/fabiobrolo))

## 📄 Licença

Distribuído sob a Licença MIT. Consulte `LICENSE` para obter mais detalhes.
