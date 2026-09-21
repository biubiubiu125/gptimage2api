import { normalizeVersionTag } from './release.ts'

export type UpdateReloadDecisionInput = {
  state: string
  taskId: string
  latestTag: string
  localVersion: string
  activeTaskId: string
  reloadedTaskId: string
}

export function shouldReloadForSucceededUpdate(input: UpdateReloadDecisionInput): boolean {
  if (input.state !== 'succeeded') return false
  const taskId = String(input.taskId || '').trim()
  const activeTaskId = String(input.activeTaskId || '').trim()
  const reloadedTaskId = String(input.reloadedTaskId || '').trim()
  if (!taskId || taskId !== activeTaskId || taskId === reloadedTaskId) return false
  const targetTag = normalizeVersionTag(input.latestTag)
  const localTag = normalizeVersionTag(input.localVersion)
  return Boolean(targetTag && localTag !== targetTag)
}
