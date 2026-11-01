<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import type { Document } from '../api'
const props = defineProps<{ document: Document; writable: boolean; onChanged: () => Promise<void> }>()
defineEmits<{ close: [] }>()
type Version = { id: string; name: string; status: string; active: boolean; created_at: string; size_bytes: number; quality_report?: { status?: string; pages?: { page: number; issues: { code: string; severity: string }[] }[] } }
const versions = ref<Version[]>([]), error = ref(''), busy = ref(false)
const source = ref(props.document.source || 'upload')
function localInput(value: string) { const date = new Date(value.endsWith('Z') ? value : value + 'Z'); return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16) }
const expires = ref(props.document.expires_at ? localInput(props.document.expires_at) : '')
let timer: ReturnType<typeof setInterval> | undefined
const labels: Record<string, string> = { ready: '已发布', queued: '排队中', processing: '处理中', needs_review: '待人工审核', failed: '处理失败' }
const issueLabels: Record<string, string> = { corrupt_text: '乱码或异常控制字符过多', suspicious_characters: '存在疑似异常字符', empty_page: '未提取到文字，请核对是否为空白页', ocr_unavailable: '文字识别未完成' }
async function refresh() { versions.value = await api(`/documents/${props.document.id}/versions`) }
async function run(action: () => Promise<unknown>) {
  busy.value = true; error.value = ''
  try { await action(); await refresh(); await props.onChanged() }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '操作失败' }
  finally { busy.value = false }
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  if (!file) return
  const form = new FormData(); form.set('file', file); form.set('knowledge_base_id', props.document.knowledge_base_id); form.set('document_id', props.document.id)
  await run(() => api('/documents/upload', { method: 'POST', body: form })); input.value = ''
}
onMounted(() => { void run(refresh); timer = setInterval(() => {
  if (!busy.value && versions.value.some(version => ['queued', 'processing'].includes(version.status))) void run(refresh)
}, 2500) })
onUnmounted(() => clearInterval(timer))
</script>
<template>
  <div class="modal-backdrop" @click.self="$emit('close')"><section class="modal versions-modal" role="dialog" aria-modal="true" aria-labelledby="versions-title">
    <div class="modal-header"><span class="eyebrow">DOCUMENT LIFECYCLE</span><button class="text-button" @click="$emit('close')">关闭</button></div>
    <h2 id="versions-title">文档版本与有效期</h2><p>{{ document.name }} · 新版本完成后切换，重建期间继续使用原版本。</p>
    <p v-if="error" role="alert" class="version-error">{{ error }}</p>
    <template v-if="writable"><label>上传更新文件<input type="file" :disabled="busy" accept=".pdf,.docx,.txt,.md,.csv,.json,.html,.png,.jpg,.jpeg,.webp" @change="upload" /></label>
      <form @submit.prevent="run(() => api(`/documents/${document.id}/metadata`, { method: 'PATCH', body: JSON.stringify({ source, expires_at: expires ? new Date(expires).toISOString() : null }) }))">
        <label>来源标识<input v-model="source" maxlength="2000" /></label><label>有效期至（本地时间，留空为长期有效）<input v-model="expires" type="datetime-local" /></label><button class="btn secondary" :disabled="busy">保存来源与有效期</button>
      </form></template>
    <div class="version-list"><article v-for="version in versions" :key="version.id"><div><strong>{{ version.name }}</strong><small>{{ new Date(version.created_at + 'Z').toLocaleString() }} · {{ labels[version.status] || version.status }} {{ version.active ? '· 当前使用' : '' }}</small><code :title="version.id">版本 {{ version.id.slice(0, 8) }}</code><details v-if="version.quality_report?.status"><summary>解析质量：{{ version.quality_report.status === 'needs_review' ? '需补充清晰文件或校正后重试' : '基础检查通过' }}</summary><p>基础检查不能确认原文事实或复杂版面阅读顺序。</p><template v-for="page in version.quality_report.pages" :key="page.page"><p v-for="(issue, index) in page.issues" :key="index">第 {{ page.page }} 页：{{ issueLabels[issue.code] || issue.code }}</p></template></details></div><button v-if="writable && !version.active" class="text-button" :disabled="busy" @click="run(() => api(`/documents/${document.id}/versions/${version.id}/restore`, { method: 'POST' }))">{{ version.status === 'needs_review' ? '重新处理（不跳过质量检查）' : '恢复此版本' }}</button></article></div>
  </section></div>
</template>
<style scoped>
.versions-modal{max-width:760px;max-height:90vh;overflow:auto}.versions-modal>p{font-size:14px;color:#718078;line-height:1.8}.versions-modal label{display:flex;flex-direction:column;gap:8px;margin:16px 0;font-size:14px}.versions-modal input{padding:10px;border:1px solid #dce5df;border-radius:8px}.version-list article{display:flex;align-items:center;justify-content:space-between;padding:16px 0;border-top:1px solid #e7ece8}.version-list small,.version-list code{display:block;font-size:14px;margin-top:6px;color:#77867d}.version-error{color:#b34747!important}
</style>
