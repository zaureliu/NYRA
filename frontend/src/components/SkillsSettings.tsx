import { useEffect, useState } from 'react'

interface Skill { name:string; description:string; permission:'READ_ONLY'|'CONFIRM_REQUIRED'|'DANGEROUS'; cooldown_seconds:number; cooldown_remaining:number; enabled:boolean; available:boolean; last_used?:number }

export function SkillsSettings() {
  const [skills, setSkills] = useState<Skill[]>([])
  const load = () => fetch('/api/skills').then((r) => r.json()).then((value) => setSkills(value.skills ?? [])).catch(() => setSkills([]))
  useEffect(() => { void load() }, [])
  const update = async (skill: Skill, patch: Partial<Skill>) => {
    const response = await fetch(`/api/skills/${skill.name}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(patch) })
    if (response.ok) void load()
  }
  return <div className="settings-group"><h3>SKILLS</h3>{skills.map((skill) => <div className="voice-card" key={skill.name}>
    <span><strong>{skill.name}</strong><small>{skill.description}</small><small>{skill.permission} · cooldown {skill.cooldown_seconds}s · {skill.available ? 'AVAILABLE' : 'UNAVAILABLE'}</small></span>
    <label><input type="checkbox" checked={skill.enabled} disabled={!skill.available || skill.permission === 'DANGEROUS'} onChange={(e) => void update(skill,{enabled:e.target.checked})}/> Enabled</label>
  </div>)}</div>
}
