#!/usr/bin/env node
'use strict';

const { ModelManager } = require('@accordproject/concerto-core');
const { CodeGen } = require('@accordproject/concerto-codegen');
const fs = require('fs');
const path = require('path');

// Single source of truth for valid jurisdictions (US states + DC + international)
const US_JURISDICTIONS = [
  "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
  "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
  "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
  "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
  "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
  "New Hampshire", "New Jersey", "New Mexico", "New York",
  "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
  "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
  "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
  "West Virginia", "Wisconsin", "Wyoming", "District of Columbia"
];

// International jurisdictions commonly used in cross-border contracts
const INTERNATIONAL_JURISDICTIONS = [
  "Republic of Singapore", "Hong Kong SAR", "England and Wales",
  "Republic of Kenya", "Republic of Indonesia", "Republic of India",
  "Kingdom of Saudi Arabia", "United Arab Emirates",
  "People's Republic of China", "Japan", "Republic of Korea",
  "Federal Republic of Nigeria", "Republic of South Africa"
];

const ALL_JURISDICTIONS = [...US_JURISDICTIONS, ...INTERNATIONAL_JURISDICTIONS];

function loadModel(templateDir) {
  const ctoPath = path.join(templateDir, 'model', 'model.cto');
  const ctoContent = fs.readFileSync(ctoPath, 'utf-8');
  const mm = new ModelManager();
  mm.addCTOModel(ctoContent, 'model.cto');
  return mm;
}

function generateSchema(templateDir) {
  const mm = loadModel(templateDir);
  const visitor = new CodeGen.formats.jsonschema();
  // Use the concerto-codegen jsonschema options (accordproject/concerto-codegen
  // #213): includeTypeTag:false drops the $class type tag we don't use at
  // runtime, and defaultsAreOptional:true excludes default-bearing fields from
  // `required`. These replace the manual post-processing this script used to do.
  const params = {
    fileWriter: new InMemoryWriter(),
    includeTypeTag: false,
    defaultsAreOptional: true,
  };
  mm.accept(visitor, params);

  const schema = JSON.parse(params.fileWriter.getContent());

  // Find the template concept definition
  const defs = schema.definitions || {};
  const conceptKey = Object.keys(defs).find(k => defs[k].$decorators && defs[k].$decorators.template);
  if (!conceptKey) {
    // Fallback: find any concept
    const keys = Object.keys(defs);
    if (keys.length === 0) throw new Error('No concepts found in model');
  }
  const concept = defs[conceptKey || Object.keys(defs)[0]];

  if (concept && concept.properties) {
    // Constrain governingLaw to the valid-jurisdiction list.
    //
    // CONDITIONAL injection: only inject the ALL_JURISDICTIONS enum when
    // governingLaw is a plain string property WITHOUT an existing enum or $ref.
    // Templates whose model.cto types governingLaw as a native Concerto enum
    // (e.g. nda-mutual's `o Jurisdiction governingLaw`) already emit a $ref to
    // a definition carrying the enum — injecting here would clobber that and
    // regress to a string. The 5 templates that still use `o String governingLaw`
    // get the enum injected as before.
    const gl = concept.properties.governingLaw;
    if (gl && !('enum' in gl) && !('$ref' in gl)) {
      gl.enum = ALL_JURISDICTIONS;
    }
  }

  const outPath = path.join(templateDir, 'schema.json');
  fs.writeFileSync(outPath, JSON.stringify(schema, null, 2) + '\n');
  console.log('Generated: ' + outPath);

  // If the model defines a native Jurisdiction enum, extract its @Display
  // decorators into a bidirectional display-map artifact next to schema.json.
  // identifier (enum value) -> human-readable display name.
  generateJurisdictionMap(mm, templateDir);

  // Multi-enum display maps (EntityType, DisputeForum, ...) for the render path.
  generateEnumDisplayMaps(mm, templateDir);

  // If a field carries @Abstainable, single-source the abstain policy (sentinel
  // + LLM instruction + representable set) into abstain-policy.json so the
  // instruction is generated from this .cto, not hand-duplicated in Python.
  generateAbstainPolicy(mm, templateDir);

  return schema;
}

// Namespace of the model that owns the Jurisdiction enum.
const NDA_NAMESPACE = 'org.openclaw.nda@1.0.0';

function generateJurisdictionMap(mm, templateDir) {
  let jurisdictionType;
  try {
    jurisdictionType = mm.getType(`${NDA_NAMESPACE}.Jurisdiction`);
  } catch {
    return; // model does not define a Jurisdiction enum; nothing to emit
  }
  if (!jurisdictionType) return;

  const displayMap = {};
  for (const prop of jurisdictionType.getProperties()) {
    const identifier = prop.getName();
    let display = identifier;
    const decorator = prop.getDecorator('Display');
    if (decorator) {
      const args = decorator.getArguments();
      if (args && args.length > 0 && typeof args[0] === 'string') {
        display = args[0];
      }
    }
    displayMap[identifier] = display;
  }

  const outPath = path.join(templateDir, 'jurisdictions.map.json');
  fs.writeFileSync(outPath, JSON.stringify(displayMap, null, 2) + '\n');
  console.log('Generated: ' + outPath);
}

// Emit a display map for EVERY @Display-bearing enum (identifier -> display name),
// keyed by enum name, so the render path can show "limited liability company" while
// the data model stores "limited_liability_company". Generalizes the Jurisdiction
// pattern to EntityType / DisputeForum / any future enum. jurisdictions.map.json is
// kept (above) for back-compat; this is the multi-enum companion.
// Fail codegen LOUD (RT7) on display-map ambiguity: the runtime builds a reverse
// {display -> identifier} map, so (a) two members sharing one display string would
// silently last-win, and (b) one member's display equaling a DIFFERENT member's
// identifier makes identifier-vs-display resolution ambiguous.
function validateEnumDisplayMap(enumName, m) {
  const seen = {};
  for (const [id, display] of Object.entries(m)) {
    if (seen[display] !== undefined) {
      throw new Error(
        `enum ${enumName}: members "${seen[display]}" and "${id}" share the same ` +
        `@Display string "${display}" -- display strings must be unique per enum.`);
    }
    seen[display] = id;
  }
  for (const [id, display] of Object.entries(m)) {
    if (display !== id && Object.prototype.hasOwnProperty.call(m, display)) {
      throw new Error(
        `enum ${enumName}: member "${id}"'s @Display string "${display}" equals a ` +
        `DIFFERENT member's identifier -- ambiguous reverse mapping; rename one.`);
    }
  }
}

function generateEnumDisplayMaps(mm, templateDir) {
  const maps = {};
  for (const file of mm.getModelFiles()) {
    for (const decl of file.getAllDeclarations()) {
      if (typeof decl.isEnum !== 'function' || !decl.isEnum()) continue;
      const m = {};
      let sawDisplay = false;
      for (const prop of decl.getProperties()) {
        const id = prop.getName();
        let display = id;
        const dec = prop.getDecorator && prop.getDecorator('Display');
        if (dec) {
          const a = dec.getArguments();
          if (a && a.length > 0 && typeof a[0] === 'string') { display = a[0]; sawDisplay = true; }
        }
        m[id] = display;
      }
      if (sawDisplay) {
        validateEnumDisplayMap(decl.getName(), m);
        maps[decl.getName()] = m;
      }
    }
  }
  if (Object.keys(maps).length === 0) return;
  const outPath = path.join(templateDir, 'enum-displays.map.json');
  fs.writeFileSync(outPath, JSON.stringify(maps, null, 2) + '\n');
  console.log('Generated: ' + outPath);
}

// Single-source the typed-abstention policy from the model. The abstain
// instruction (previously hand-duplicated in demo_mars_beat._ABSTAIN_SYSTEM,
// because @description does NOT survive codegen into the JSON Schema) is read
// from an @Abstainable("<sentinel>", "<instruction>") decorator on the field
// and emitted to abstain-policy.json. This is PROVENANCE/drift-guard, not a
// behavior change: the instruction text is byte-identical to what was hardcoded.
//
// Mirrors generateJurisdictionMap: read the model, validate, write our own file.
// The decorator drives a SIDE-ARTIFACT -- it does NOT (and cannot, in Concerto)
// synthesize the sentinel enum member or the companion field; those stay in the
// .cto. Validation runs fully BEFORE the write so an invalid decorator fails
// loud without leaving a stale artifact.
// Validate one @Abstainable field and return its policy entry. Throws (fail loud)
// on any inconsistency so an invalid decorator never produces a silent wrong policy.
function buildFieldPolicy(mm, owner, abField) {
  // 1. Decorator must carry exactly two string args: sentinel + instruction.
  const args = abField.getDecorator('Abstainable').getArguments();
  if (!Array.isArray(args) || args.length !== 2 ||
      typeof args[0] !== 'string' || typeof args[1] !== 'string') {
    throw new Error(
      `@Abstainable on ${owner.getName()}.${abField.getName()} must have exactly two ` +
      `string arguments: ("<sentinel>", "<instruction>"). Got: ${JSON.stringify(args)}`);
  }
  const sentinel = args[0];
  const instruction = args[1];
  if (!instruction.trim()) {
    throw new Error(`@Abstainable instruction on ${abField.getName()} must be a non-empty string.`);
  }

  // 2. The abstainable field must be a native enum; resolve it and read members.
  const enumType = mm.getType(abField.getFullyQualifiedTypeName());
  if (!enumType || typeof enumType.getProperties !== 'function' || !enumType.isEnum?.()) {
    throw new Error(
      `@Abstainable field ${abField.getName()} must be a native enum type; ` +
      `${abField.getFullyQualifiedTypeName()} is not.`);
  }
  const members = enumType.getProperties().map(p => p.getName());

  // 3. The enum TYPE must also carry the bare @Abstainable marker (the type declares
  //    the capability, the field declares the policy).
  if (!enumType.getDecorator('Abstainable')) {
    throw new Error(
      `enum ${enumType.getName()} backs an @Abstainable field but is not itself ` +
      `marked @Abstainable. Add the bare @Abstainable marker to the enum declaration.`);
  }

  // 4. Sentinel must be a real member of the enum (the in-schema abstain value).
  if (!members.includes(sentinel)) {
    throw new Error(
      `@Abstainable sentinel "${sentinel}" is not a member of enum ${enumType.getName()}. ` +
      `The sentinel must exist in the .cto enum (codegen cannot synthesize it).`);
  }

  // 5. The companion raw field (<field>Raw) must exist for human handoff.
  const rawName = abField.getName() + 'Raw';
  if (typeof owner.getProperty !== 'function' || !owner.getProperty(rawName)) {
    throw new Error(
      `@Abstainable requires a companion field "${rawName}" on ${owner.getName()} ` +
      `(carries the verbatim un-representable ask for human review).`);
  }

  // 6. Representable set = every enum member EXCEPT the sentinel, in enum order.
  return {
    sentinel,
    rawField: rawName,
    enum: enumType.getName(),
    instruction,
    representable: members.filter(m => m !== sentinel),
  };
}

function generateAbstainPolicy(mm, templateDir) {
  // Collect EVERY @Abstainable field (a template can carry several -- governingLaw,
  // entityType, disputeForum...). Each gets its own policy entry, keyed by field name.
  const policies = {};
  for (const file of mm.getModelFiles()) {
    for (const decl of file.getAllDeclarations()) {
      if (typeof decl.getProperties !== 'function') continue;
      for (const prop of decl.getProperties()) {
        if (prop.getDecorator && prop.getDecorator('Abstainable')) {
          policies[prop.getName()] = buildFieldPolicy(mm, decl, prop);
        }
      }
    }
  }

  const outPath = path.join(templateDir, 'abstain-policy.json');
  if (Object.keys(policies).length === 0) {
    // No @Abstainable field. Delete any stale artifact so the sidecar tracks the .cto;
    // the consumer then fails closed on the missing artifact, which is correct.
    if (fs.existsSync(outPath)) {
      fs.unlinkSync(outPath);
      console.log('Removed stale: ' + outPath);
    }
    return;
  }

  fs.writeFileSync(outPath, JSON.stringify({ policies }, null, 2) + '\n');
  console.log('Generated: ' + outPath);
}

function generateTypescript(templateDir) {
  const mm = loadModel(templateDir);
  const visitor = new CodeGen.formats.typescript();
  const writer = new InMemoryWriter();
  const params = { fileWriter: writer };
  mm.accept(visitor, params);

  const typesDir = path.join(templateDir, 'types');
  fs.mkdirSync(typesDir, { recursive: true });
  for (const [name, content] of Object.entries(writer.files)) {
    const outPath = path.join(typesDir, name);
    fs.writeFileSync(outPath, content);
    console.log('Generated: ' + outPath);
  }
}

// Simple in-memory file writer for codegen visitors
class InMemoryWriter {
  constructor() {
    this.files = {};
    this._current = null;
    this._lines = [];
  }
  openFile(name) {
    if (this._current) this.closeFile();
    this._current = name;
    this._lines = [];
  }
  writeLine(indent, line) {
    this._lines.push(' '.repeat(indent || 0) + (line || ''));
  }
  closeFile() {
    if (this._current) {
      this.files[this._current] = this._lines.join('\n');
    }
    this._current = null;
    this._lines = [];
  }
  getContent() {
    // Return all content as a single string (for single-file outputs like JSON Schema)
    this.closeFile();
    return Object.values(this.files).join('\n');
  }
}

// CLI
const args = process.argv.slice(2);
const templatesRoot = path.join(__dirname, '..', 'data', 'templates', 'cicero');

// `--all` iterates every template directory under data/templates/cicero/ and
// generates BOTH schema and TypeScript for each.
// Otherwise: first positional arg selects one template, `--schema`/`--typescript`
// filters the outputs.
const allTemplates = args.includes('--all');
const explicitDir = args.find(a => !a.startsWith('-'));

const doSchema = args.includes('--schema') || allTemplates || args.length === 0 || !args.some(a => a.startsWith('--'));
const doTypescript = args.includes('--typescript') || allTemplates;

function listTemplateDirs() {
  return fs.readdirSync(templatesRoot)
    .map(name => path.join(templatesRoot, name))
    .filter(p => {
      try {
        return fs.statSync(p).isDirectory() &&
               fs.existsSync(path.join(p, 'model', 'model.cto'));
      } catch { return false; }
    });
}

function processTemplate(templateDir) {
  if (doSchema) generateSchema(templateDir);
  if (doTypescript) generateTypescript(templateDir);
}

try {
  if (allTemplates && !explicitDir) {
    const dirs = listTemplateDirs();
    if (dirs.length === 0) {
      throw new Error(`No template directories with model.cto found under ${templatesRoot}`);
    }
    for (const d of dirs) processTemplate(d);
    console.log(`\nProcessed ${dirs.length} template(s).`);
  } else {
    const targetDir = explicitDir || path.join(templatesRoot, 'nda-mutual');
    processTemplate(targetDir);
  }
} catch (err) {
  console.error('Error:', err.message);
  process.exit(1);
}
