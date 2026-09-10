import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const root = process.cwd()
const read = path => readFileSync(join(root, path), 'utf8')
const assert = (condition, message) => {
  if (!condition) {
    throw new Error(message)
  }
}

const packageJson = JSON.parse(read('package.json'))
assert(packageJson.name === 'gptimage2api-console', 'package name must stay gptimage2api-console')

const registerApi = read('src/api/register.ts')
assert(registerApi.includes("'/api/register'"), 'register API client must call /api/register')
assert(!registerApi.includes('/api/register/gptmail'), 'register API client must not expose removed provider runtime endpoints')

const imageTasks = read('src/api/imageTasks.ts')
assert(imageTasks.includes('return localImageUrl(asset.path)'), 'image task assets must render path-only results')
assert(imageTasks.includes('cancel: boolean'), 'image task actions must expose cancellation state')
assert(imageTasks.includes('`/api/image-tasks/${encodeURIComponent(taskId)}/cancel`'), 'image task API must call the cancel endpoint')
assert(imageTasks.includes('cancel: async (taskId: string)'), 'image task API must expose cancel action')

const imageTaskRuntime = read('src/views/studio/studioImageTaskRuntime.ts')
assert(imageTaskRuntime.includes('async function cancelTask'), 'Studio image runtime must expose task cancellation')
assert(imageTaskRuntime.includes('imageTasksApi.cancel(normalizedTaskId)'), 'Studio image runtime must call image task cancellation')
assert(imageTaskRuntime.includes('storedImageTaskIds()'), 'Studio image runtime must restore persisted task IDs')
assert(imageTaskRuntime.includes("'partial_success'"), 'Studio image runtime must treat partial task results as terminal')
assert(imageTasks.includes("'partial_success'"), 'image task API must expose partial success status')

const studioMessageItem = read('src/components/studio/StudioMessageItem.vue')
assert(studioMessageItem.includes("'cancel-image-task'"), 'Studio message actions must expose image task cancellation')

const studioMessageList = read('src/components/studio/StudioMessageList.vue')
assert(studioMessageList.includes("'cancel-image-task': [message: StudioMessage]"), 'Studio message list must forward image task cancellation')

const studioView = read('src/views/Studio.vue')
assert(studioView.includes('@cancel-image-task="cancelImageTask"'), 'Studio view must wire image task cancellation')
assert(studioView.includes('imageTaskRuntime.cancelTask(message.taskId)'), 'Studio view must invoke image task cancellation')

const routes = read('src/router/routes.ts')
assert(routes.includes("path: 'register'"), 'router must expose /register')

const registerLiveRuntime = read('src/views/register/registerLiveRuntime.ts')
assert(registerLiveRuntime.includes("eventName !== 'message'"), 'register SSE must ignore non-message events')
assert(registerLiveRuntime.includes("line.startsWith('event:')"), 'register SSE must parse event names')

const appShell = read('src/layouts/AppShell.vue')
assert(appShell.includes('/register'), 'sidebar must link to /register')
assert(appShell.includes('GPTImage2API'), 'shell must use GPTImage2API branding')

const providerView = read('src/views/register/registerProviderView.ts')
const providerValues = [...providerView.matchAll(/value: '([^']+)'/g)].map(match => match[1])
const allowed = ['yyds_mail', 'remail', 'outlook_token', 'icloud_api']
for (const provider of allowed) {
  assert(providerValues.includes(provider), `provider selector missing ${provider}`)
}
const unexpectedProviders = providerValues.filter(provider => !allowed.includes(provider)
  && !['total', 'quota', 'available', 'global', 'direct', 'group', 'custom', 'code', 'purchase', 'private_first', 'public_only', 'graph', 'imap', 'auto'].includes(provider))
assert(unexpectedProviders.length === 0, `unexpected provider options: ${unexpectedProviders.join(', ')}`)

const filesToScan = [
  'src/api/register.ts',
  'src/views/Register.vue',
  'src/views/register/RegisterProviderCard.vue',
  'src/views/register/registerProviderRuntime.ts',
  'src/views/register/registerProviderView.ts',
]
const removedTokens = [
  'cloudmail_gen',
  'cloudflare_temp_email',
  'tempmail_lol',
  'moemail',
  'inbucket',
  'duckmail',
  'gptmail',
  'donemail',
  'ddg_mail',
  'GPTMail',
  'CloudMail',
  'DoneMail',
  'DDG',
]
for (const file of filesToScan) {
  const content = read(file)
  for (const token of removedTokens) {
    assert(!content.includes(token), `${file} still exposes removed provider token ${token}`)
  }
}

console.log('gptimage2api frontend runtime contract ok')
