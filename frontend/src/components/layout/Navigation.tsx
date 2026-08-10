import type React from "react"
import { useEffect, useRef, useState } from "react"
import { NavLink, useLocation } from "react-router-dom"
import { useTranslation } from "react-i18next"
import {
  LayoutDashboardIcon,
  BuildingIcon,
  PlugIcon,
  HistoryIcon,
  FileTextIcon,
  CalendarIcon,
  SettingsIcon,
  BotIcon,
  UserPlusIcon,
  ZapIcon,
  SendIcon,
  GitBranchIcon,
  ShieldAlertIcon,
  ShieldCheckIcon,
  NetworkIcon,
  ActivityIcon,
  PackageXIcon,
  HeartPulseIcon,
  LayoutTemplateIcon,
  SparklesIcon,
  XIcon,
} from "lucide-react"
import { useAuth } from "@/contexts/AuthContext"
import { eeNavItems } from "@/ee/navItems"
import type { EeNavItem, NavGroupKey } from "@/ee/navItems"
import { usePermission } from "@/hooks/usePermission"
import { cn } from "@/lib/utils"

/**
 * Navegação organizada pelo ESTÁGIO DO PIPELINE.
 *
 * A ordem do pipeline é a ordem do menu, e a matiz de cada grupo é a mesma
 * matiz que o estágio tem no resto do produto (coleta = âmbar, normaliza =
 * violeta, reduz = teal, roteia = ciano, detecta = vermelhão). A barra de 2px à
 * esquerda é o único device estrutural: sem numeral, sem ícone de grupo, sem
 * segunda barra por item. A ordem e a matiz já carregam a sequência.
 *
 * ÂNCORA: visão geral (Dashboard, Saúde do pipeline, Histórico) fica FIXA no
 * topo, fora da área que rola. Dashboard é a âncora do console; caía abaixo da
 * dobra num notebook de 1280×720 e sumia sem nenhum aviso. Agora nunca sai da
 * tela, e a sequência do pipeline começa logo abaixo dela.
 *
 * O que não é estágio (administração) fica num bloco neutro abaixo de uma
 * divisória, sem matiz — console ocioso é neutro. O que sobra do rail rola,
 * com névoa nas duas pontas quando há mais conteúdo naquela direção.
 */

type StageKey = "collect" | "normalize" | "enrich" | "reduce" | "route" | "detect"

interface NavItem {
  key: string
  label: string
  path: string
  icon: React.ReactNode
}

interface NavGroup {
  key: NavGroupKey
  label: string
  /** Ausente = grupo neutro (sem barra de estágio). */
  stage?: StageKey
  items: NavItem[]
}

/**
 * Classe literal por estágio — o scanner do Tailwind precisa da string inteira
 * no código; nome de classe montado em runtime não gera utility.
 */
const STAGE_BAR: Record<StageKey, string> = {
  collect: "bg-stage-collect",
  normalize: "bg-stage-normalize",
  enrich: "bg-stage-enrich",
  reduce: "bg-stage-reduce",
  route: "bg-stage-route",
  detect: "bg-stage-detect",
}

interface NavigationProps {
  /** Drawer aberto (mobile/tablet, <lg). */
  open?: boolean
  onClose?: () => void
  /** Rail só-ícones no desktop (>=lg). Não afeta o drawer mobile. */
  collapsed?: boolean
}

/**
 * Item de navegação. Ativo = preenchimento elevado + texto claro; a barra de
 * acento por item saiu para não competir com a barra de estágio do grupo.
 * No rail colapsado (lg) vira só-ícone, com o rótulo acessível via aria-label.
 */
const NavItemLink: React.FC<{ item: NavItem; collapsed: boolean; onNavigate?: () => void }> = ({
  item,
  collapsed,
  onNavigate,
}) => (
  <li>
    <NavLink
      to={item.path}
      onClick={onNavigate}
      aria-label={item.label}
      title={collapsed ? item.label : undefined}
      className={({ isActive }) =>
        cn(
          // py-1.5 (linha de 32px, alvo de toque ≥24px da WCAG 2.5.8) no lugar
          // de py-2: 19 links num rail de 664px não sobrevivem a 36px por linha.
          "flex items-center gap-3 rounded-md px-3 py-1.5 text-sm transition-colors",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500",
          collapsed && "lg:justify-center lg:gap-0 lg:px-0",
          isActive
            ? "bg-sidebar-active font-medium text-sidebar-text-active"
            : "text-sidebar-text hover:bg-sidebar-hover hover:text-sidebar-text-active",
        )
      }
    >
      <span className="shrink-0" aria-hidden="true">
        {item.icon}
      </span>
      <span className={cn("truncate", collapsed && "lg:hidden")}>{item.label}</span>
    </NavLink>
  </li>
)

export const Navigation: React.FC<NavigationProps> = ({ open = false, onClose, collapsed = false }) => {
  const { t } = useTranslation("nav")
  const { user } = useAuth()
  const location = useLocation()
  const isAdmin = user?.role === "admin"
  const canManageUsers = usePermission("user.manage")
  const canRunQuery = usePermission("query.run")
  const canSaveQuery = usePermission("query.save")
  const navRef = useRef<HTMLElement>(null)
  const previousActive = useRef<HTMLElement | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const [overflow, setOverflow] = useState({ top: false, bottom: false })

  // Névoa de rolagem: mede o container de verdade em vez de adivinhar. Roda no
  // scroll e a cada mudança de tamanho — trocar de idioma, colapsar o rail ou
  // ganhar/perder um link por permissão muda a altura do conteúdo.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return

    const update = () => {
      const slack = el.scrollHeight - el.clientHeight
      setOverflow({ top: el.scrollTop > 1, bottom: slack - el.scrollTop > 1 })
    }

    update()
    el.addEventListener("scroll", update, { passive: true })

    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update)
    observer?.observe(el)
    // O primeiro filho é o conteúdo: o container só muda de caixa quando a
    // JANELA muda, e um link a mais precisa reacender a névoa do mesmo jeito.
    if (el.firstElementChild) observer?.observe(el.firstElementChild)

    return () => {
      el.removeEventListener("scroll", update)
      observer?.disconnect()
    }
  }, [collapsed])

  // No drawer mobile: ao abrir, move o foco para dentro e o prende (Tab/Shift+Tab
  // circulam); ESC fecha; ao fechar, RESTAURA o foco ao elemento que abriu
  // (gatilho hambúrguer) — convenção de dialog modal (WCAG 2.4.3).
  useEffect(() => {
    if (!open) return
    const node = navRef.current
    previousActive.current = document.activeElement as HTMLElement | null

    const getFocusables = () =>
      Array.from(
        node?.querySelectorAll<HTMLElement>('a[href], button, [tabindex]:not([tabindex="-1"])') ?? [],
      ).filter((el) => !el.hasAttribute("disabled") && el.offsetParent !== null)

    getFocusables()[0]?.focus()

    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose?.()
        return
      }
      if (e.key !== "Tab") return
      const focusables = getFocusables()
      if (focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener("keydown", handler)

    return () => {
      document.removeEventListener("keydown", handler)
      const previous = previousActive.current
      if (previous && typeof previous.focus === "function" && previous.offsetParent !== null) {
        previous.focus()
      }
    }
  }, [open, onClose])

  const groups: NavGroup[] = [
    {
      // Âncora do console — renderizada FIXA no topo, antes dos estágios.
      key: "overview",
      label: t("navigation.groups.overview"),
      items: [
        { key: "dashboard", label: t("navigation.items.dashboard"), path: "/dashboard", icon: <LayoutDashboardIcon size={18} /> },
        { key: "health", label: t("navigation.items.health"), path: "/pipeline-health", icon: <HeartPulseIcon size={18} /> },
        { key: "history", label: t("navigation.items.history"), path: "/history", icon: <HistoryIcon size={18} /> },
      ],
    },
    {
      key: "collect",
      stage: "collect",
      label: t("navigation.groups.collect"),
      items: [
        { key: "integrations", label: t("navigation.items.integrations"), path: "/integrations", icon: <PlugIcon size={18} /> },
        { key: "collectors", label: t("navigation.items.collectors"), path: "/collectors", icon: <ZapIcon size={18} /> },
      ],
    },
    {
      key: "normalize",
      stage: "normalize",
      label: t("navigation.groups.normalize"),
      items: [
        { key: "mappings", label: t("navigation.items.mappings"), path: "/mappings", icon: <LayoutTemplateIcon size={18} /> },
        { key: "drift", label: t("navigation.items.drift"), path: "/drift", icon: <ActivityIcon size={18} /> },
        { key: "quarantine", label: t("navigation.items.quarantine"), path: "/quarantine", icon: <PackageXIcon size={18} /> },
        // Governança OCSF mora em /admin/ocsf, mas o que ela controla é a
        // normalização (classes e versão OCSF aceitas) — por isso vem aqui.
        ...(isAdmin
          ? [{ key: "ocsf", label: t("navigation.items.ocsf"), path: "/admin/ocsf", icon: <ShieldCheckIcon size={18} /> }]
          : []),
      ],
    },
    {
      // Enriquece — entre normalizar e reduzir, que é a ordem real no pipeline:
      // o enriquecimento roda sobre o envelope já normalizado e ANTES do
      // fan-out/redução, para que a detecção em voo enxergue o contexto.
      key: "enrich",
      stage: "enrich",
      label: t("navigation.groups.enrich"),
      items: isAdmin
        ? [
            {
              key: "enrichment",
              label: t("navigation.items.enrichment"),
              path: "/enrichment",
              icon: <SparklesIcon size={18} />,
            },
          ]
        : [],
    },
    {
      key: "reduce",
      stage: "reduce",
      label: t("navigation.groups.reduce"),
      // Sem tela dedicada de redução/custo hoje: os números de economia vivem
      // dentro de /flow (CostSavingsCard). O grupo fica declarado como costura —
      // grupo vazio não renderiza (ver `visibleGroups`) — e recebe a primeira
      // tela de redução que existir, no Community ou no overlay EE.
      items: [],
    },
    {
      key: "route",
      stage: "route",
      label: t("navigation.groups.route"),
      items: isAdmin
        ? [
            { key: "routes", label: t("navigation.items.routes"), path: "/routes", icon: <GitBranchIcon size={18} /> },
            { key: "destinations", label: t("navigation.items.destinations"), path: "/destinations", icon: <SendIcon size={18} /> },
            { key: "flow", label: t("navigation.items.flow"), path: "/flow", icon: <NetworkIcon size={18} /> },
          ]
        : [],
    },
    {
      key: "detect",
      stage: "detect",
      label: t("navigation.groups.detect"),
      items: [
        ...(canRunQuery
          ? [{ key: "detections", label: t("navigation.items.detections"), path: "/detections", icon: <ShieldAlertIcon size={18} /> }]
          : []),
        // Query salva + agendamento é o que ALIMENTA a detecção no Community.
        { key: "queries", label: t("navigation.items.queries"), path: "/queries", icon: <FileTextIcon size={18} /> },
        ...(isAdmin
          ? [{ key: "schedules", label: t("navigation.items.schedules"), path: "/schedules", icon: <CalendarIcon size={18} /> }]
          : []),
      ],
    },
    {
      key: "admin",
      label: t("navigation.groups.administration"),
      items: [
        ...(isAdmin
          ? [{ key: "organizations", label: t("navigation.items.organizations"), path: "/organizations", icon: <BuildingIcon size={18} /> }]
          : []),
        ...(canManageUsers
          ? [
              { key: "admin-users", label: t("navigation.items.adminUsers"), path: "/admin/users", icon: <UserPlusIcon size={18} /> },
              {
                key: "service-accounts",
                label: t("navigation.items.serviceAccounts"),
                path: "/admin/service-accounts",
                icon: <BotIcon size={18} />,
              },
            ]
          : []),
        ...(isAdmin ? [{ key: "config", label: t("navigation.items.config"), path: "/config", icon: <SettingsIcon size={18} /> }] : []),
      ],
    },
    {
      key: "account",
      label: t("navigation.groups.account"),
      // Minha conta e Tokens saíram do rail: o UserMenu (avatar, canto superior
      // direito) já leva às MESMAS duas rotas, e o rail não tinha altura para
      // pagar duas vezes pelo mesmo destino. O grupo fica declarado como costura
      // do overlay EE — grupo vazio não renderiza (ver `visibleGroups`).
      items: [],
    },
  ]

  // Entradas Enterprise (busca federada, correlação). Vazio no Community (o stub
  // @/ee/navItems) → nenhuma entrada para rotas que o bundle Community não traz.
  const eeExtra = eeNavItems({ canRunQuery, canSaveQuery, isAdmin }) as Record<string, EeNavItem[] | undefined>
  for (const g of groups) {
    // Chave estável primeiro; rótulo traduzido como compatibilidade com overlays
    // antigos, que era o contrato anterior (e quebrava ao trocar de idioma).
    const extra = eeExtra[g.key] ?? eeExtra[g.label]
    if (extra?.length) g.items.push(...extra)
  }

  const visibleGroups = groups.filter((g) => g.items.length > 0)
  const anchorGroup = visibleGroups.find((g) => g.key === "overview")
  const stageGroups = visibleGroups.filter((g) => g.stage)
  const neutralGroups = visibleGroups.filter((g) => !g.stage && g.key !== "overview")

  // Estágio da rota atual: a barra do grupo em que você está acende por inteiro;
  // as outras ficam em contexto. Foco/contexto, não decoração.
  const isPathActive = (path: string) => location.pathname === path || location.pathname.startsWith(`${path}/`)
  const activeGroupKey = visibleGroups.find((g) => g.items.some((i) => isPathActive(i.path)))?.key

  const renderGroup = (group: NavGroup) => (
    // No rail, o padding direito iguala o esquerdo para o ícone cair no centro
    // exato da coluna de 64px (senão a barra de estágio o empurra 2px).
    <div key={group.key} className={cn("relative mb-3 pl-3 pr-2", collapsed && "lg:pr-3")}>
      {group.stage && (
        <span
          aria-hidden="true"
          className={cn(
            "absolute bottom-0 left-1 top-0 w-0.5 rounded-full transition-opacity",
            STAGE_BAR[group.stage],
            group.key === activeGroupKey ? "opacity-100" : "opacity-40",
          )}
        />
      )}
      <div
        className={cn(
          "mb-1 px-3 font-mono text-[10px] font-medium uppercase leading-4 tracking-[0.18em] text-sidebar-group",
          collapsed && "lg:hidden",
        )}
      >
        {group.label}
      </div>
      {/* O rótulo mono é visual; o aria-label é o que amarra a lista ao grupo
          para o leitor de tela (e continua valendo no rail, onde ele some). */}
      <ul className="flex flex-col gap-0.5" aria-label={group.label}>
        {group.items.map((item) => (
          <NavItemLink key={item.key} item={item} collapsed={collapsed} onNavigate={onClose} />
        ))}
      </ul>
    </div>
  )

  return (
    <nav
      ref={navRef}
      id="primary-navigation"
      className={cn(
        "flex w-56 shrink-0 flex-col overflow-hidden bg-sidebar py-3",
        // Comportamento de drawer abaixo de lg
        "fixed inset-y-0 left-0 z-modal transition-[transform,width] duration-200 ease-out",
        "lg:static lg:z-auto lg:translate-x-0",
        collapsed ? "lg:w-16" : "lg:w-56",
        open ? "translate-x-0 shadow-xl lg:shadow-none" : "-translate-x-full",
      )}
      role={open ? "dialog" : undefined}
      aria-label={t("navigation.ariaLabel")}
      aria-modal={open ? "true" : undefined}
    >
      {/* Botão de fechar — só no drawer mobile */}
      <div className="mb-2 flex justify-end px-4 lg:hidden">
        <button
          type="button"
          onClick={onClose}
          aria-label={t("header.closeMenu")}
          className="rounded-md p-1.5 text-sidebar-text transition-colors hover:bg-sidebar-hover hover:text-sidebar-text-active focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
        >
          <XIcon size={20} />
        </button>
      </div>

      {/* Âncora fixa: fora do container que rola, então Dashboard e Saúde do
          pipeline estão sempre na tela. Sem rótulo mono — é o topo do rail, não
          um estágio; o nome do bloco vai no aria-label da lista. */}
      {anchorGroup && (
        <div className={cn("shrink-0 pl-3 pr-2", collapsed && "lg:pr-3")}>
          <ul className="flex flex-col gap-0.5" aria-label={anchorGroup.label}>
            {anchorGroup.items.map((item) => (
              <NavItemLink key={item.key} item={item} collapsed={collapsed} onNavigate={onClose} />
            ))}
          </ul>
          <div aria-hidden="true" className="mx-1 my-3 h-px bg-border" />
        </div>
      )}

      {/* Área que rola + névoa nas pontas. A névoa só aparece do lado em que há
          mais conteúdo: é a affordance de que o rail continua. */}
      <div className="relative min-h-0 flex-1">
        <div ref={scrollRef} className="h-full overflow-y-auto overflow-x-hidden scrollbar-thin">
          {/* Um único filho: é ele que o ResizeObserver mede para saber se ainda
              há rail abaixo da dobra. */}
          <div>
            {stageGroups.map(renderGroup)}

            {stageGroups.length > 0 && neutralGroups.length > 0 && (
              <div aria-hidden="true" className="mx-4 mb-3 h-px bg-border" />
            )}

            {neutralGroups.map(renderGroup)}
          </div>
        </div>

        <div
          aria-hidden="true"
          className={cn(
            "pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-sidebar to-sidebar/0 transition-opacity",
            overflow.top ? "opacity-100" : "opacity-0",
          )}
        />
        <div
          aria-hidden="true"
          className={cn(
            "pointer-events-none absolute inset-x-0 bottom-0 h-6 bg-gradient-to-t from-sidebar to-sidebar/0 transition-opacity",
            overflow.bottom ? "opacity-100" : "opacity-0",
          )}
        />
      </div>
    </nav>
  )
}
