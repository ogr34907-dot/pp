import type { CharacterDTO, LocationDTO, StyleNoteDTO } from '@/api/bible'

export interface BiblePanelCharacterInput {
  name: string
  role: string
  traits: string
  arc_note: string
}

export interface BiblePanelLocationInput {
  name: string
  description: string
}

export interface BiblePanelSaveInput {
  characters: BiblePanelCharacterInput[]
  locations: BiblePanelLocationInput[]
  style_notes: string
}

export function toBibleApiPayload(novelId: string, data: BiblePanelSaveInput) {
  const characters: CharacterDTO[] = data.characters.map((character, index) => ({
    id: `${novelId}-char-${index + 1}`,
    name: character.name || '',
    description: [character.role, character.traits, character.arc_note].filter(Boolean).join('\n---\n'),
    relationships: [],
  }))

  const locations: LocationDTO[] = data.locations.map((location, index) => ({
    id: `${novelId}-loc-${index + 1}`,
    name: location.name || '',
    description: location.description || '',
    location_type: 'general',
  }))

  const style_notes: StyleNoteDTO[] = data.style_notes
    ? [{
        id: `${novelId}-style-1`,
        category: 'general',
        content: data.style_notes,
      }]
    : []

  return { characters, world_settings: [], locations, timeline_notes: [], style_notes }
}
