'use strict';
const $ = id => document.getElementById(id);
const groups = {
  persona: ['人格与表达','PERSONALITY','给机器人一个名字，调节每一次回应的温度。'],
  ai: ['模型与连接','INTELLIGENCE','连接兼容 OpenAI 的模型服务，控制生成质量与调用预算。'],
  platform: ['平台与账号','CONNECTION','设置 B 站账号校验、采集节奏和请求限制。'],
  limits: ['额度与边界','BOUNDARIES','分别管理私信、评论额度，以及白名单和回复长度。'],
  discovery: ['视频发现','DISCOVERY','通过关键词发现视频，邀请你在意的人一起观看。'],
  publishing: ['发布控制','PUBLISHING','默认模拟演练。真实发布需要关闭模拟，并开启发布许可。'],
  evidence: ['字幕与证据','UNDERSTANDING','设置视频理解范围、字幕语言和内容缓存。'],
  transcription: ['音频转写','TRANSCRIPTION','无字幕时的独立转写服务；在「字幕与证据」中开启。'],
  local: ['本地模型','ON DEVICE','Apple Silicon 原生推理：语音、视觉和文本任务串行执行，每次完成后释放模型。'],
  vision: ['画面理解','VISION','均匀抽取各分 P 的画面，结合字幕或语音理解视频；抽样可能遗漏短暂事件。'],
  runtime: ['运行调度','RUNTIME','控制处理批次、状态更新与安全停止的等待时间。'],
  general: ['存储与安全','WORKSPACE','管理运行数据位置，以及额外拦截的敏感词。']
};
const labels = {
  backend:'推理方式',speech_model:'本地语音模型',memory_gb:'MLX 内存预算（GB）',context_tokens:'上下文总 Token 上限',
  enabled:'开启画面理解',max_frames:'整个视频最大帧数',frames_per_batch:'每批画面数',long_edge:'画面最长边（像素）',download_timeout:'视频下载超时（秒）',
  name:'机器人名字',personality:'性格描述',warmth:'温暖程度',humor:'幽默感',empathy:'共情程度',
  base_url:'服务地址',model:'模型名称',api_key:'API 密钥',temperature:'采样温度',timeout:'超时（秒）',
  max_tokens:'最大输出 Token',token_parameter:'Token 参数名称',structured_output:'结构化输出方式',
  send_temperature:'发送温度参数',retries:'有限重试次数',max_response_bytes:'最大响应（字节）',
  max_calls_per_minute:'每分钟最大调用数',max_input_chars:'最大输入字符',allow_insecure_http:'允许 HTTP 服务',
  bot_uid:'机器人 UID',request_timeout:'平台请求超时（秒）',read_interval:'读取间隔（秒）',
  write_interval:'写入间隔（秒）',poll_interval:'采集间隔（秒）',refresh_interval:'凭据续期间隔（秒）',
  max_pages:'每轮最大页数',history_lookback_seconds:'首次历史回溯（秒）',dm_per_hour:'每人每小时私信额度',
  comment_per_hour:'每人每小时评论额度',whitelist:'额度白名单 UID',concurrency:'最大并发数',
  max_message_chars:'最大消息字符',max_reply_chars:'最大回复字符',keywords:'搜索关键词',invite_uids:'邀请用户 UID',
  interval:'发现间隔（秒）',pages_per_keyword:'每个关键词的搜索页数',videos_per_cycle:'每轮视频数量',
  dry_run:'模拟演练',publish_enabled:'允许真实发布',cache_ttl:'缓存有效期（秒）',max_video_seconds:'最大视频时长（秒）',
  max_download_mb:'视频总下载预算（MB）',max_text_chars:'最大文本字符',transcription_enabled:'开启音频转写',
  transcription_model:'转写模型',subtitle_languages:'字幕语言优先级',comment_sample_size:'热门评论样本数',
  language:'语言提示',max_upload_bytes:'单次上传上限（字节）',max_calls_per_video:'每个视频最大转写次数',
  backend_id:'转写后端标识',worker_interval:'工作循环间隔（秒）',batch_size:'每批任务数',
  shutdown_timeout:'安全停止等待（秒）',status_interval:'状态更新间隔（秒）',data_dir:'数据目录',unsafe_words:'额外拦截词'
};
const hints = {
  backend:'api 使用已配置的服务；local_mlx 使用本机模型，无需服务地址和 API 密钥。',
  memory_gb:'约 20GB 可分配内存建议保留 4GB 余量。此项限制 MLX 分配，不是整机内存硬上限。',
  context_tokens:'输入（含图片 Token）与输出的总预算；超限会停止，不会截断后假装完整理解。',
  max_frames:'所有分 P 共用此预算；按分 P 均分，并在各段内均匀采样。',
  enabled:'使用本地视觉模型；首次需安装本地依赖并下载模型。视频下载仍需联网。',
  name:'用于回复中的身份表达，不修改 B 站昵称。',personality:'描述你希望的语气、性格和陪伴方式。',
  bot_uid:'0 表示使用扫码账号；其他值必须与实际登录账号一致。',
  api_key:'留空保留已保存的密钥。密钥不会显示在页面或接口响应中。',
  whitelist:'每行一个数字 UID。仅豁免额度，仍须通过关注与安全检查。',
  invite_uids:'每行一个数字 UID。系统会核对账号名称并构造真实 @。',
  keywords:'每行一个关键词。没有邀请用户时，不运行视频搜索。',
  subtitle_languages:'每行一种语言，按从上到下的顺序优先选择。',
  unsafe_words:'每行一个词；保存后加入输入安全检查。',
  data_dir:'相对路径以启动工作目录为准。更换目录不会迁移账号和历史数据，可能需要重新扫码。',
  dry_run:'开启后只模拟评论、私信和点赞；采集与模型调用仍会联网。',
  publish_enabled:'只有同时关闭模拟演练才会真实发送；保存会重启运行中的机器人。',
  allow_insecure_http:'只在明确需要的可信内网服务中使用；传输不加密。',
  language:'两位语言代码，例如 zh；留空让服务自动识别。',
  transcription_enabled:'开启后在「音频转写」选择本地推理，或填写 API 服务和独立密钥。'
};
let token = new URLSearchParams(location.hash.slice(1)).get('token') || sessionStorage.getItem('bot-console-token') || '';
history.replaceState(null,'',location.pathname);
let latestState;
let data, draft, section = 'persona', dirty = false, busy = false, qrUrl, showHelp = false;
const controls = new Map();
function notice(text){$('notice').textContent=text;$('notice').hidden=false;}
async function api(path, body){
  const response=await fetch(path,{method:body?'POST':'GET',headers:{Authorization:`Bearer ${token}`,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});
  const value=await response.json();
  if(!response.ok){
    if(response.status===401){$('access').hidden=false;$('workspace').hidden=true;}
    if(value.fields){
      const first=value.fields[0].path.split('.')[0];
      if(groups[first])selectSection(first);
      for(const error of value.fields){const node=controls.get(error.path);if(node)node.closest('.field').classList.add('invalid');}
    }
    throw new Error(value.error+(value.fields?'\n'+value.fields.map(x=>`${x.path}：${x.type}`).join('\n'):''));
  }
  return value;
}
function changed(){dirty=true;$('save-state').textContent='未保存';$('notice').hidden=true;}
function selectSection(key){section=key;render();}
function render(){
  if(!draft)return;
  $('nav').replaceChildren();
  Object.entries(groups).forEach(([key,[title]],i)=>{
    const button=document.createElement('button');button.type='button';button.className=key===section?'active':'';
    button.setAttribute('aria-current',key===section?'page':'false');
    const number=document.createElement('span');number.className='nav-number';number.textContent=String(i+1).padStart(2,'0');
    button.append(number,document.createTextNode(title));button.onclick=()=>selectSection(key);$('nav').append(button);
  });
  const [title,kicker,description]=groups[section];
  $('section-title').textContent=title;$('section-kicker').textContent=kicker;$('section-description').textContent=description;
  const schema=section==='general'?data.schema:data.schema.$defs[data.schema.properties[section].$ref.split('/').pop()];
  const entries=Object.entries(schema.properties).filter(([key])=>section!=='general'||['data_dir','unsafe_words'].includes(key));
  $('fields').replaceChildren();controls.clear();
  if(section==='local'){
    const card=document.createElement('div');card.className='field';
    const copy=document.createElement('div');const title=document.createElement('strong');title.textContent='20GB 推荐方案';
    const hint=document.createElement('small');hint.textContent='Qwen 4B · Whisper Turbo';copy.append(title,hint);
    const setup=document.createElement('details');const summary=document.createElement('summary');summary.textContent='安装说明';const instructions=document.createElement('small');instructions.textContent='先安装：uv sync --extra local；再下载：uv run --extra local bili-comment-bot prepare-local-models。模型推理在本机完成，视频采集仍需联网。';setup.append(summary,instructions);copy.append(setup);
    const button=document.createElement('button');button.type='button';button.textContent='应用方案';button.disabled=busy;
    button.onclick=()=>{
      draft.ai.backend='local_mlx';draft.ai.max_tokens=1800;draft.ai.retries=0;
      draft.transcription.backend='local_mlx';draft.evidence.transcription_enabled=true;
      draft.local={model:'mlx-community/Qwen3-VL-4B-Instruct-4bit',speech_model:'mlx-community/whisper-large-v3-turbo',memory_gb:16,timeout:900,context_tokens:8192};
      draft.vision={enabled:true,max_frames:24,frames_per_batch:4,long_edge:768,max_tokens:600,download_timeout:120};
      changed();render();notice('本地方案已填入，保存后生效。请先完成依赖安装和模型下载。');
    };card.append(copy,button);$('fields').append(card);
  }
  for(const [key,rule] of entries){
    const path=section==='general'?key:`${section}.${key}`,value=section==='general'?draft[key]:draft[section][key];
    const row=document.createElement('div');row.className='field';const copy=document.createElement('div');
    const label=document.createElement('label');label.htmlFor=path;label.textContent=labels[key]||key;
    const hint=document.createElement('small');hint.id=`${path}-hint`;
    const range=(rule.minimum!==undefined?`最小 ${rule.minimum}`:rule.exclusiveMinimum!==undefined?`大于 ${rule.exclusiveMinimum}`:'')+(rule.maximum!==undefined?` · 最大 ${rule.maximum}`:'');
    hint.textContent=hints[key]||range;hint.className='field-hint';hint.hidden=!showHelp||!hint.textContent;copy.append(label,hint);
    const wrap=document.createElement('div');let input;
    if(rule.enum||rule.const!==undefined){input=document.createElement('select');for(const item of (rule.enum||[rule.const])){const option=document.createElement('option');option.value=item;option.textContent=({api:'API 服务',local_mlx:'本机 MLX'})[item]||item;input.append(option);}input.value=value;}
    else if(rule.type==='array'||key==='personality'){input=document.createElement('textarea');input.value=Array.isArray(value)?value.join('\n'):value;}
    else{input=document.createElement('input');input.type=rule.type==='boolean'?'checkbox':rule.type==='number'||rule.type==='integer'?'number':key==='api_key'?'password':'text';
      if(input.type==='checkbox')input.checked=value;else input.value=value??'';
      if(input.type==='number'){input.step=rule.type==='integer'?'1':'any';if(rule.minimum!==undefined)input.min=rule.minimum;if(rule.maximum!==undefined)input.max=rule.maximum;}
      if(rule.maxLength)input.maxLength=rule.maxLength;if(rule.minLength)input.minLength=rule.minLength;
      if(key==='api_key'){input.autocomplete='new-password';input.placeholder=data.secrets[section]?'已保存 · 留空保持不变':'输入服务 API 密钥';}
    }
    input.disabled=busy;input.id=path;input.setAttribute('aria-describedby',hint.id);controls.set(path,input);
    input.oninput=()=>{
      row.classList.remove('invalid');let next=input.type==='checkbox'?input.checked:input.type==='number'?(input.value===''?null:Number(input.value)):input.value;
      if(rule.type==='array'){next=input.value.split('\n').map(x=>x.trim()).filter(Boolean);if(rule.items.type==='integer')next=next.map(x=>/^\d+$/.test(x)&&Number.isSafeInteger(Number(x))?Number(x):x);}
      if(section==='general')draft[key]=next;else draft[section][key]=next;changed();
    };
    wrap.append(input);
    if(key==='api_key'){
      const tools=document.createElement('div');tools.className='secret-tools';tools.textContent=data.secrets[section]?'已保存密钥':'尚未保存密钥';
      const clear=document.createElement('button');clear.type='button';clear.disabled=busy;clear.textContent='清除密钥';clear.onclick=()=>{draft[section][key]=null;input.value='';input.placeholder='保存时清除密钥';changed();};tools.append(clear);wrap.append(tools);
    }
    row.append(copy,wrap);$('fields').append(row);
  }
}
function showState(value){
  latestState=value;
  const states={stopped:'已停止',running:'运行中',stopping:'正在停止',login:'等待扫码',error:'需要处理'};
  $('state').textContent=states[value.state]||value.state;
  if(value.state==='running'&&value.status&&!value.status.healthy)$('state').textContent='启动中 / 需要关注';
  $('message').textContent=value.message;$('message').hidden=!(['error','login','stopping'].includes(value.state)||(value.status&&!value.status.healthy));$('mode').textContent=value.mode==='live'?'真实发布':'模拟演练';
  $('qr-card').hidden=value.state!=='login';
  $('start').disabled=busy||['running','login','stopping'].includes(value.state);
  $('stop').disabled=busy||!['running','login','stopping'].includes(value.state);
}
async function load(){
  try{data=await api('/api/state');draft=structuredClone(data.settings);dirty=false;sessionStorage.setItem('bot-console-token',token);$('access').hidden=true;$('workspace').hidden=false;$('notice').hidden=true;$('save-state').textContent='已同步';render();showState(data);}
  catch(error){notice(error.message);if(!data)$('access').hidden=false;}
}
async function action(route,body={}){
  if(busy)return;busy=true;$('form').inert=true;for(const id of ['save','reset','start','stop','login'])$(id).disabled=true;
  try{
    const result=await api(route,body);
    if(route==='/api/config'){data=result;draft=structuredClone(data.settings);dirty=false;render();$('save-state').textContent='已保存 · '+new Date().toLocaleTimeString();}
    showState(result);notice(result.message);
  }catch(error){notice(error.message);}
  finally{busy=false;$('form').inert=false;for(const input of $('form').querySelectorAll('input,textarea,select,button'))input.disabled=false;for(const id of ['save','reset','login'])$(id).disabled=false;if(latestState)showState(latestState);}
}
$('help-toggle').onclick=()=>{showHelp=!showHelp;$('help-toggle').textContent=showHelp?'收起说明':'显示说明';$('help-toggle').setAttribute('aria-pressed',String(showHelp));$('section-description').hidden=!showHelp;render();};
$('connect').onclick=()=>{token=$('token').value.trim();load();};
$('token').onkeydown=event=>{if(event.key==='Enter')$('connect').click();};
$('form').onsubmit=event=>event.preventDefault();
$('reset').onclick=()=>{if(!dirty||confirm('放弃所有未保存的更改？'))load();};
$('save').onclick=()=>{
  if(!$('form').reportValidity())return;
  if(draft.publishing.publish_enabled&&!draft.publishing.dry_run&&(data.settings.publishing.dry_run||!data.settings.publishing.publish_enabled)&&!confirm('启用真实发布后，机器人会实际发送评论、私信和点赞。确认保存？'))return;
  action('/api/config',{settings:draft,revision:data.revision});
};
$('start').onclick=()=>{if(dirty){notice('请先保存或放弃更改，再启动机器人。');return;}action('/api/start');};
$('stop').onclick=()=>action('/api/stop');
$('login').onclick=()=>{if(dirty){notice('请先保存配置，再扫码登录。');return;}action('/api/login');};
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
async function poll(){
  if(data&&!busy){try{
    const state=await api('/api/status');showState(state);
    if(state.revision!==data.revision)notice('配置已在其他页面更新。请重新加载后编辑，以免覆盖。');
    if(state.state==='login'){
      const response=await fetch('/api/qr',{headers:{Authorization:`Bearer ${token}`}});
      if(response.ok){const next=URL.createObjectURL(await response.blob());$('qr').src=next;if(qrUrl)URL.revokeObjectURL(qrUrl);qrUrl=next;}
    }else{$('qr').removeAttribute('src');if(qrUrl){URL.revokeObjectURL(qrUrl);qrUrl=null;}}
  }catch(error){notice(error.message);}}
  setTimeout(poll,3000);
}
if(token)load();else $('access').hidden=false;
poll();
