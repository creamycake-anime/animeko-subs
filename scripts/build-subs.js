#!/usr/bin/env node

const fs = require("node:fs");
const path = require("node:path");

const rootDir = path.resolve(__dirname, "..");
const args = process.argv.slice(2);

function readArgValue(name, defaultValue) {
  const index = args.indexOf(name);
  if (index === -1) {
    return defaultValue;
  }

  const value = args[index + 1];
  if (!value || value.startsWith("--")) {
    throw new Error(`${name} requires a path`);
  }

  return value;
}

const checkOnly = args.includes("--check");
const outputDir = path.resolve(rootDir, readArgValue("--out-dir", "v1"));

const targets = [
  {
    name: "css1",
    inputDir: path.join(rootDir, "subs", "web"),
    outputFile: path.join(outputDir, "css1.json"),
    recursive: true,
    sortByTier: true,
    enforceTierDirs: true,
    order: [
      "omofun111.json",
      "风铃动漫.json",
      "叽哔动漫.json",
      "E-ACG.json",
      "稀饭动漫.json",
      "森之屋动漫.json",
      "风车影视.json",
      "去看吧.json",
      "海星动漫.json",
      "樱花动漫.json",
      "第一动漫.json",
      "次元方舟.json",
      "米粒动漫.json",
      "萌道动漫.json",
      "UZVOD.json",
      "嘀哩嘀哩.json",
      "2k动漫.json",
      "新优酷.json",
      "hanime1[1080p].json",
      "hanime1[720p].json",
      "girigiri愛動漫.json",
      "风车动漫.json",
      "趣动漫.json",
      "稀饭动漫2.json",
      "咕咕番.json",
      "喵物次元.json",
      "虾皮动漫.json",
      "漫次元.json",
      "蜜桃动漫.json",
      "次元城动画.json",
      "虾皮动漫2.json",
      "漫次元2.json",
      "蜜桃动漫2.json",
      "MX动漫.json",
      "动漫蛋.json",
      "饭团动漫.json",
      "番茄动漫.json",
    ],
  },
  {
    name: "api1",
    inputDir: path.join(rootDir, "subs", "api"),
    outputFile: path.join(outputDir, "api1.json"),
    recursive: true,
    sortByTier: true,
    enforceTierDirs: true,
    order: [],
  },
  {
    name: "bt1",
    inputDir: path.join(rootDir, "subs", "bt"),
    outputFile: path.join(outputDir, "bt1.json"),
    recursive: false,
    sortByTier: false,
    order: [
      "nyaa.land.json",
      "AnimeGarden.json",
    ],
  },
];

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    error.message = `${file}: ${error.message}`;
    throw error;
  }
}

function assertMediaSource(source, file) {
  if (!source || typeof source !== "object" || Array.isArray(source)) {
    throw new Error(`${file}: expected a media source object`);
  }
  for (const key of ["factoryId", "version", "arguments"]) {
    if (!(key in source)) {
      throw new Error(`${file}: missing required field "${key}"`);
    }
  }
  if (!source.arguments || typeof source.arguments !== "object" || Array.isArray(source.arguments)) {
    throw new Error(`${file}: expected "arguments" to be an object`);
  }
  if (typeof source.arguments.name !== "string" || source.arguments.name.length === 0) {
    throw new Error(`${file}: expected "arguments.name" to be a non-empty string`);
  }
}

function sourceTier(source, file) {
  const tier = source.arguments.tier;

  if (Number.isInteger(tier) && tier >= 0) {
    return tier;
  }

  throw new Error(`${file}: expected "arguments.tier" to be a non-negative integer`);
}

function assertTierDirectory(entry, target) {
  if (!target.enforceTierDirs) {
    return;
  }

  const expectedDir = `t${entry.tier}`;
  const actualDir = entry.relativePath.split(path.sep)[0];
  if (actualDir !== expectedDir) {
    throw new Error(`${entry.file}: expected to be under ${path.join(target.inputDir, expectedDir)}`);
  }
}

function listJsonFiles(dir, recursive) {
  if (!fs.existsSync(dir)) {
    throw new Error(`${dir}: input directory does not exist`);
  }

  const files = [];
  const entries = fs.readdirSync(dir, { withFileTypes: true });

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory() && recursive) {
      files.push(...listJsonFiles(fullPath, recursive));
    } else if (entry.isFile() && entry.name.endsWith(".json")) {
      files.push(fullPath);
    }
  }

  return files.sort((a, b) => a.localeCompare(b));
}

function buildTarget(target) {
  const order = new Map(target.order.map((fileName, index) => [fileName, index]));
  const entries = listJsonFiles(target.inputDir, target.recursive).map((file) => {
    const source = readJson(file);
    assertMediaSource(source, file);
    const entry = {
      file,
      fileName: path.basename(file),
      relativePath: path.relative(target.inputDir, file),
      source,
      tier: target.sortByTier ? sourceTier(source, file) : 0,
    };
    assertTierDirectory(entry, target);
    return entry;
  });

  entries.sort((a, b) => {
    if (target.sortByTier && a.tier !== b.tier) {
      return a.tier - b.tier;
    }

    const aIndex = order.get(a.fileName);
    const bIndex = order.get(b.fileName);

    if (aIndex !== undefined && bIndex !== undefined) {
      return aIndex - bIndex;
    }
    if (aIndex !== undefined) {
      return -1;
    }
    if (bIndex !== undefined) {
      return 1;
    }

    return a.relativePath.localeCompare(b.relativePath);
  });

  return {
    exportedMediaSourceDataList: {
      mediaSources: entries.map(({ source }) => source),
    },
  };
}

let changed = false;

for (const target of targets) {
  const output = `${JSON.stringify(buildTarget(target), null, 2)}\n`;

  if (checkOnly) {
    const current = fs.existsSync(target.outputFile)
      ? fs.readFileSync(target.outputFile, "utf8")
      : "";
    if (current !== output) {
      console.error(`${path.relative(rootDir, target.outputFile)} is out of date`);
      changed = true;
    }
  } else {
    fs.mkdirSync(path.dirname(target.outputFile), { recursive: true });
    fs.writeFileSync(target.outputFile, output);
    console.log(`built ${path.relative(rootDir, target.outputFile)}`);
  }
}

if (changed) {
  process.exitCode = 1;
}
