<template>
  <section class="scenario-panel" data-testid="export-credential-panel">
    <div class="panel-head">
      <h3>交付凭据 · 口径附注与关联键复验</h3>
    </div>
    <p class="panel-tip">
      导出通道只消费实际绑定的存量快照生成口径附注；交付清单按凭据冻结，回滚后仍可按实际交付复算。
    </p>

    <!-- 凭据回查 / 继续办理 -->
    <div class="sub-box">
      <h4 class="block-title">凭交付凭据回查材料、配置、附件并继续未办申请</h4>
      <div class="cred-row">
        <input
          v-model="credentialInput"
          class="cred-input"
          data-testid="credential-input"
          placeholder="输入交付凭据，如 DLV-L09 / CRD-DATE-0715"
        />
        <button class="btn primary" type="button" data-testid="credential-lookup-btn" @click="lookupCredential">
          凭据回查/复验
        </button>
        <button class="btn" type="button" data-testid="credential-continue-btn" @click="continueCredential">
          凭凭据继续办理
        </button>
        <button class="btn ghost" type="button" data-testid="quick-date-only-btn" @click="useCredential('CRD-DATE-0715')">
          只有日期的旧任务
        </button>
      </div>
      <p v-if="credMessage" class="panel-message" data-testid="credential-message" :data-ok="credOk">
        {{ credMessage }}
      </p>

      <!-- 未交付申请（如只有日期的旧任务） -->
      <div v-if="continueInfo" class="continue-box" data-testid="continue-result" :data-continued="continueInfo.continued">
        <p><strong>已定位申请 #{{ continueInfo.task_id }}：</strong>{{ continueInfo.task?.任务名称 }}</p>
        <p>状态：{{ continueInfo.status }}；可生成口径附注：{{ continueInfo.can_generate_note ? '是' : '否' }}</p>
        <ul v-if="continueInfo.blockers?.length" class="blocker-list" data-testid="continue-blockers">
          <li v-for="(b, i) in continueInfo.blockers" :key="i">{{ b }}</li>
        </ul>
      </div>

      <!-- 已交付冻结清单 -->
      <div v-if="manifest" class="manifest-box" data-testid="credential-detail" :data-credential="manifest.credential">
        <div class="manifest-head">
          <strong>凭据 <code data-testid="manifest-credential">{{ manifest.credential }}</code></strong>
          <span class="status-tag">修订 v{{ manifest.revision }} · 已冻结</span>
        </div>
        <p class="note-text" data-testid="credential-note">{{ manifest.note }}</p>
        <p class="meta-line">
          日期 {{ manifest.date }} ｜ 币对 {{ manifest.pair }} ｜
          实际交付汇率 <strong data-testid="manifest-rate">{{ manifest.rate_text || manifest.rate }}</strong> ｜
          来源：<span data-testid="manifest-source">{{ manifest.source || '来源缺失' }}</span>
        </p>
        <p class="meta-line">
          快照关联键 <code>{{ manifest.snapshot_key }}</code>
          <template v-if="manifest.resolves_to"> ｜ 外部键跳转 <code>{{ manifest.resolves_to }}</code></template>
        </p>

        <details open class="config-details">
          <summary>回查配置</summary>
          <pre class="json-pre" data-testid="credential-config">{{ JSON.stringify(manifest.config, null, 2) }}</pre>
        </details>

        <div class="attach-row" data-testid="attachment-list">
          <span>附件：</span>
          <a
            v-for="att in manifest.attachments"
            :key="att.name"
            :href="att.url"
            class="att-link"
            :data-testid="`attachment-${att.kind}`"
            target="_blank"
            rel="noopener"
          >
            {{ att.name }}（{{ att.description || att.kind }}）
          </a>
        </div>

        <div class="recompute-row" data-testid="recompute-box">
          <label>复算金额
            <input v-model.number="recomputeAmount" type="number" data-testid="recompute-amount" />
          </label>
          <button class="btn primary" type="button" data-testid="recompute-btn" @click="doRecompute">
            按实际交付结果复算
          </button>
          <p v-if="recomputeResult" class="panel-message inline" data-testid="recompute-result" :data-ok="recomputeResult.ok">
            {{ recomputeResult.message }}
            <template v-if="recomputeResult.ok">
              ：{{ recomputeResult.amount }} × {{ recomputeResult.rate }} =
              <strong>{{ recomputeResult.converted }}</strong>
            </template>
          </p>
        </div>
      </div>
    </div>

    <!-- 关联键定位 -->
    <div class="sub-box">
      <h4 class="block-title">外部旧格式 / 关联键找真实换算来源</h4>
      <div class="cred-row">
        <input v-model="assocInput" class="cred-input" data-testid="association-input"
               placeholder="输入关联键，如 ASC-EXT-OLD-02" />
        <button class="btn primary" type="button" data-testid="association-btn" @click="lookupAssociation">
          关联键定位
        </button>
        <button class="btn ghost" type="button" @click="useAssoc('ASC-EXT-OLD-02')">外部旧格式键</button>
        <button class="btn ghost" type="button" @click="useAssoc('ASC-DATE-20260715-ONLY')">缺币对旧任务键</button>
      </div>
      <div v-if="assocResult" class="assoc-box" data-testid="association-result"
           :data-found="assocResult.found" :data-consumable="assocResult.consumable">
        <p :class="assocResult.ok ? 'ok-line' : 'warn-line'">{{ assocResult.message }}</p>
        <p v-if="assocResult.found" class="meta-line">
          类型 {{ assocResult.kind }} ｜ 日期/币对 {{ assocResult.date }} {{ assocResult.pair }} ｜
          汇率 {{ assocResult.rate ?? '—' }} ｜ 来源 {{ assocResult.source || '（无可用来源）' }}
          <template v-if="assocResult.credential"> ｜ 凭据 {{ assocResult.credential }}</template>
        </p>
      </div>
    </div>

    <!-- 同一导出：选版本 -> 绑定 -> 生成附注 -> 交付签发新凭据 -->
    <div class="sub-box">
      <h4 class="block-title">继续同一导出：选择实际绑定的存量快照版本并交付</h4>
      <div class="cred-row">
        <label class="task-id-label">导出任务 #
          <input v-model.number="taskId" type="number" data-testid="bind-task-id" class="tiny-input" />
        </label>
        <button class="btn" type="button" data-testid="load-task-versions-btn" @click="loadPairVersions">
          载入 2026-08-09 USD/CNY 版本
        </button>
      </div>
      <div class="bind-versions" data-testid="bind-version-list">
        <label v-for="v in pairVersions" :key="v.entry_key" class="bind-version"
               :data-testid="`bind-option-${v.provider_code || v.entry_key}`">
          <input type="radio" name="bind-version" :value="v.entry_key" v-model="selectedKey"
                 data-testid="bind-radio" />
          <span><strong>{{ v.rate_text || v.rate }}</strong> · {{ v.version_label }} ·
            {{ v.status }} · 来源：{{ v.source }}</span>
        </label>
      </div>
      <div class="cred-row">
        <button class="btn" type="button" data-testid="bind-note-btn" @click="bindAndNote(false)">
          绑定所选快照并生成口径附注
        </button>
        <button class="btn primary" type="button" data-testid="deliver-btn" @click="bindAndNote(true)">
          绑定并交付（签发新凭据）
        </button>
      </div>
      <p v-if="bindMessage" class="panel-message" data-testid="bind-message" :data-ok="bindOk">{{ bindMessage }}</p>
      <div v-if="deliverResult" class="manifest-box" data-testid="deliver-result"
           :data-credential="deliverResult.credential">
        <p class="ok-line"><strong>{{ deliverResult.message }}</strong></p>
        <p class="note-text" data-testid="delivered-note">{{ deliverResult.note }}</p>
        <button class="btn" type="button" data-testid="goto-new-credential-btn"
                @click="openCredential(deliverResult.credential)">
          用新凭据回查/复算
        </button>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

import { q, scenarioGet, scenarioPost } from '@/api/scenario-service'

const props = defineProps<{ tick?: number }>()
const emit = defineEmits<{ (event: 'changed'): void }>()

watch(() => props.tick, (value, old) => {
  // 外部（通用表格动作）触发时重载版本；跳过自身事件造成的同值/初始化。
  if (value !== old) void loadPairVersions()
})

const credentialInput = ref('DLV-L09')
const credMessage = ref('')
const credOk = ref(true)
const manifest = ref<any>(null)
const recomputeAmount = ref<number>(100)
const recomputeResult = ref<any>(null)
const continueInfo = ref<any>(null)

const assocInput = ref('ASC-EXT-OLD-02')
const assocResult = ref<any>(null)

const taskId = ref<number>(4)
const pairVersions = ref<any[]>([])
const selectedKey = ref('ASC-20260809-USDCNY-N10')
const bindMessage = ref('')
const bindOk = ref(true)
const deliverResult = ref<any>(null)

function useCredential(value: string) {
  credentialInput.value = value
  void lookupCredential()
}

function openCredential(value: string) {
  credentialInput.value = value
  void lookupCredential()
}

async function lookupCredential() {
  const cred = credentialInput.value.trim()
  if (!cred) return
  recomputeResult.value = null
  continueInfo.value = null
  const data = await scenarioGet(`/price_export/credential/${encodeURIComponent(cred)}`)
  if (data.found && data.manifest) {
    manifest.value = data.manifest
    credOk.value = true
    credMessage.value = data.message || '凭据复验通过'
  } else if (data.found && data.task) {
    manifest.value = null
    credOk.value = true
    credMessage.value = data.message || '该凭据对应尚未交付的申请，可继续办理'
  } else {
    manifest.value = null
    credOk.value = false
    credMessage.value = data.message || '凭据不存在'
  }
}

async function continueCredential() {
  const cred = credentialInput.value.trim()
  const data = await scenarioPost('/price_export/continue', { credential: cred })
  continueInfo.value = data
  credOk.value = data.ok !== false
  credMessage.value = data.message
  if (data.task_id) taskId.value = Number(data.task_id)
  emit('changed')
}

async function doRecompute() {
  const cred = manifest.value?.credential || credentialInput.value.trim()
  recomputeResult.value = await scenarioPost(
    `/price_export/credential/${encodeURIComponent(cred)}/recompute`,
    { amount: recomputeAmount.value },
  )
}

function useAssoc(value: string) {
  assocInput.value = value
  void lookupAssociation()
}

async function lookupAssociation() {
  const key = assocInput.value.trim()
  assocResult.value = await scenarioGet(
    `/price_export/association/${encodeURIComponent(key)}`,
  )
}

async function loadPairVersions() {
  const data = await scenarioGet(
    `/currency_conv/versions${q({ date: '2026-08-09', pair: 'USD/CNY' })}`,
  )
  pairVersions.value = data.versions || []
}

async function bindAndNote(deliver: boolean) {
  deliverResult.value = null
  bindMessage.value = ''
  const key = selectedKey.value
  const bind = await scenarioPost(`/price_export/${taskId.value}/bind`, {
    association_key: key,
    generate_note: true,
  })
  if (!bind.ok) {
    bindOk.value = false
    bindMessage.value = bind.message || bind.reason || '绑定失败'
    return
  }
  bindOk.value = true
  bindMessage.value = bind.message
  if (!deliver) {
    emit('changed')
    return
  }
  const done = await scenarioPost(`/price_export/${taskId.value}/deliver`, {})
  if (!done.ok) {
    bindOk.value = false
    bindMessage.value = done.message || '交付失败'
    return
  }
  deliverResult.value = done
  bindMessage.value = done.message
  emit('changed')
}

onMounted(loadPairVersions)
</script>

<style scoped>
.scenario-panel { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin: 12px 0; }
.panel-head h3 { margin: 0; font-size: 16px; }
.panel-tip { color: var(--muted); font-size: 12.5px; margin: 6px 0 10px; }
.sub-box { border-top: 1px dashed var(--border); padding-top: 10px; margin-top: 12px; }
.block-title { font-size: 14px; margin: 4px 0 8px; }
.cred-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.cred-input { flex: 1; min-width: 260px; border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px; font-size: 13px; }
.tiny-input { width: 70px; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; }
.task-id-label { font-size: 13px; color: #475467; }
.panel-message { font-size: 13px; padding: 8px 10px; border-radius: 6px; background: #ecfdf3; color: #027a48; margin: 8px 0; }
.panel-message[data-ok='false'] { background: #fef3f2; color: #b42318; }
.panel-message.inline { display: inline-block; margin-left: 10px; }
.continue-box, .manifest-box, .assoc-box { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin-top: 10px; background: #fcfcfd; font-size: 13px; }
.blocker-list { margin: 6px 0; padding-left: 18px; color: #b45309; }
.manifest-head { display: flex; justify-content: space-between; align-items: center; }
.status-tag { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: #d1fadf; color: #027a48; }
.note-text { background: #f8fafc; border-left: 3px solid var(--brand); padding: 8px 10px; border-radius: 4px; line-height: 1.7; margin: 8px 0; }
.meta-line { font-size: 12.5px; color: #344054; margin: 4px 0; word-break: break-all; }
.config-details summary { cursor: pointer; font-size: 12.5px; color: var(--muted); }
.json-pre { background: #1e293b; color: #e2e8f0; border-radius: 6px; padding: 10px; font-size: 12px; overflow-x: auto; }
.attach-row { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; margin: 8px 0; }
.att-link { color: var(--brand); text-decoration: none; }
.att-link:hover { text-decoration: underline; }
.recompute-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 8px; }
.recompute-row input { width: 110px; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; }
.ok-line { color: #027a48; font-size: 13px; margin: 4px 0; }
.warn-line { color: #b42318; font-size: 13px; margin: 4px 0; }
.bind-versions { display: flex; flex-direction: column; gap: 6px; margin: 8px 0; }
.bind-version { display: flex; gap: 8px; align-items: flex-start; font-size: 12.5px; border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px; cursor: pointer; }
.bind-version:has(input:checked) { border-color: var(--brand); background: #eff6ff; }
</style>
