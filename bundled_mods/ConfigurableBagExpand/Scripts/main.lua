--[[
  ConfigurableBagExpand
  Expand player Common inventory toward a configurable slot count.
  Config: Scripts/config.lua  (managed by 幻兽帕鲁 Mod 管理面板)
]]

local okCfg, cfg = pcall(function()
    return require("config")
end)
if not okCfg or type(cfg) ~= "table" then
    cfg = {}
end

local TARGET_SLOTS = tonumber(cfg.inventory_slots) or 100
local INTERVAL_SEC = tonumber(cfg.apply_on_interval_sec) or 5
local BOOST_POUCH = cfg.boost_pouch_bonus
if BOOST_POUCH == nil then
    BOOST_POUCH = true
end

if TARGET_SLOTS < 42 then TARGET_SLOTS = 42 end
if TARGET_SLOTS > 300 then TARGET_SLOTS = 300 end

local INV_TYPE_COMMON = 0 -- EPalPlayerInventoryType::Common
local lastApply = 0
local pouchBoosted = false

local function log(msg)
    print(string.format("[ConfigurableBagExpand] %s", tostring(msg)))
end

local function safeNum(container)
    if not container then return 0 end
    local ok, n = pcall(function()
        if container.Num then
            return container:Num()
        end
        return 0
    end)
    if ok and type(n) == "number" then
        return n
    end
    return 0
end

local function getItemSlotArray(container)
    if not container then return nil end
    local ok, arr = pcall(function()
        return container.ItemSlotArray
    end)
    if ok then return arr end
    return nil
end

local function tryAddSlot(container)
    local arr = getItemSlotArray(container)
    if not arr then return false end

    -- Prefer cloning an existing empty slot template
    local template = nil
    pcall(function()
        if container.Get then
            template = container:Get(0)
        end
    end)

    local slotClass = nil
    if template and template.GetClass then
        pcall(function()
            slotClass = template:GetClass()
        end)
    end
    if not slotClass then
        pcall(function()
            slotClass = StaticFindObject("/Script/Pal.PalItemSlot")
        end)
    end
    if not slotClass then
        local sample = FindFirstOf("PalItemSlot")
        if sample and sample.GetClass then
            pcall(function()
                slotClass = sample:GetClass()
            end)
        end
    end
    if not slotClass then
        return false
    end

    local newSlot = nil
    pcall(function()
        newSlot = StaticConstructObject(slotClass, container)
    end)
    if not newSlot then
        return false
    end

    local added = false
    -- UE4SS TArray helpers vary by version
    pcall(function()
        if arr.Add then
            arr:Add(newSlot)
            added = true
        end
    end)
    if not added then
        pcall(function()
            local n = arr:GetArrayNum()
            arr:Set(n, newSlot) -- some builds
            added = true
        end)
    end
    if not added then
        pcall(function()
            -- Last resort: append via metamethod if supported
            arr[#arr + 1] = newSlot
            added = true
        end)
    end
    return added
end

local function expandContainer(container, target)
    if not container then return false end
    local current = safeNum(container)
    if current >= target then
        return true
    end
    local guard = 0
    while current < target and guard < 400 do
        guard = guard + 1
        if not tryAddSlot(container) then
            break
        end
        current = safeNum(container)
    end
    local final = safeNum(container)
    if final > current or final >= target then
        log(string.format("Common inventory slots: %d -> target %d (now %d)", current, target, final))
    end
    return final >= target
end

local function expandPlayerInventory(inv)
    if not inv then return end
    -- TryGetContainerFromInventoryType(inventoryType, OutContainer)
    local container = nil
    pcall(function()
        local out = {}
        local ok = inv:TryGetContainerFromInventoryType(INV_TYPE_COMMON, out)
        if type(out) == "table" and out[1] then
            container = out[1]
        elseif type(out) == "userdata" then
            container = out
        end
        -- Some UE4SS versions return container as second value
        if not container and type(ok) == "userdata" then
            container = ok
        end
    end)

    if not container then
        pcall(function()
            local helper = inv.InventoryMultiHelper
            if helper and helper.Containers then
                local arr = helper.Containers
                local n = 0
                pcall(function()
                    n = arr:GetArrayNum()
                end)
                if n > 0 and arr.Get then
                    container = arr:Get(0)
                end
            end
        end)
    end

    if container then
        expandContainer(container, TARGET_SLOTS)
    end
end

local function applyAllInventories()
    local list = FindAllOf("PalPlayerInventoryData")
    if not list then
        list = FindAllOf("BP_PalPlayerInventoryData_C")
    end
    if not list then return end
    for _, inv in pairs(list) do
        pcall(function()
            expandPlayerInventory(inv)
        end)
    end
end

local function boostPouchItems()
    if not BOOST_POUCH or pouchBoosted then return end
    local items = FindAllOf("PalStaticItemDataBase")
    if not items then return end
    local bonus = math.max(1, math.floor((TARGET_SLOTS - 42) / 4))
    local count = 0
    for _, item in pairs(items) do
        pcall(function()
            local id = ""
            if item.GetId then
                id = tostring(item:GetId() or "")
            elseif item.ID then
                id = tostring(item.ID)
            end
            id = string.lower(id)
            if id:find("additionalinventory") or id:find("pouch") or id:find("inventorybag") then
                -- FloatValue1 used by some pouch definitions as extra slots
                if item.FloatValue1 ~= nil then
                    local v = tonumber(item.FloatValue1) or 0
                    if v < bonus then
                        item.FloatValue1 = bonus * 1.0
                        count = count + 1
                    end
                end
            end
        end)
    end
    pouchBoosted = true
    if count > 0 then
        log(string.format("Boosted %d pouch-like items (bonus≈%d)", count, bonus))
    end
end

local function apply()
    boostPouchItems()
    applyAllInventories()
    lastApply = os.clock()
end

-- When possession is acknowledged (player ready)
RegisterHook("/Script/Engine.PlayerController:ServerAcknowledgePossession", function()
    ExecuteWithDelay(1500, function()
        apply()
    end)
end)

RegisterHook("/Script/Engine.PlayerController:ClientRestart", function()
    ExecuteWithDelay(2000, function()
        apply()
    end)
end)

NotifyOnNewObject("/Script/Pal.PalPlayerInventoryData", function(inv)
    ExecuteWithDelay(1000, function()
        pcall(function()
            expandPlayerInventory(inv)
        end)
    end)
end)

-- Periodic re-apply (containers can re-init)
if INTERVAL_SEC and INTERVAL_SEC > 0 then
    LoopAsync(math.floor(INTERVAL_SEC * 1000), function()
        apply()
        return false -- keep looping
    end)
end

log(string.format("Loaded. target_slots=%d interval=%ds pouch_boost=%s",
    TARGET_SLOTS, INTERVAL_SEC, tostring(BOOST_POUCH)))
