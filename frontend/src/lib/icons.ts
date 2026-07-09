import {
  Activity,
  AlertTriangle,
  Bug,
  CheckCircle2,
  Clock,
  Cloud,
  Cpu,
  Database,
  Info,
  Network,
  Server,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Target,
  Users,
  XCircle,
  type LucideIcon,
} from "lucide-react"

const ICON_MAP: Record<string, LucideIcon> = {
  alert: AlertTriangle,
  shield: Shield,
  "shield-check": ShieldCheck,
  "shield-alert": ShieldAlert,
  activity: Activity,
  server: Server,
  cpu: Cpu,
  users: Users,
  database: Database,
  network: Network,
  cloud: Cloud,
  clock: Clock,
  target: Target,
  bug: Bug,
  check: CheckCircle2,
  x: XCircle,
  info: Info,
}

export const iconFor = (id?: string | null): LucideIcon =>
  (id != null && ICON_MAP[id]) || Info
