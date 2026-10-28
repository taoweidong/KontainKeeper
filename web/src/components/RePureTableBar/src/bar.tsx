import Sortable from "sortablejs";
import type { CheckboxValueType } from "element-plus";
import { useEpThemeStoreHook } from "@/store/modules/epTheme";
import {
  type PropType,
  ref,
  unref,
  computed,
  nextTick,
  defineComponent,
  getCurrentInstance
} from "vue";
import {
  delay,
  cloneDeep,
  isBoolean,
  isFunction,
  getKeyList
} from "@pureadmin/utils";

import PinAngle from "~icons/bi/pin-angle";
import Fullscreen from "~icons/ri/fullscreen-fill";
import PinAngleFill from "~icons/bi/pin-angle-fill";
import ExitFullscreen from "~icons/ri/fullscreen-exit-fill";
import DragIcon from "@/assets/table-bar/drag.svg?component";
import ExpandIcon from "@/assets/table-bar/expand.svg?component";
import RefreshIcon from "@/assets/table-bar/refresh.svg?component";
import SettingIcon from "@/assets/table-bar/settings.svg?component";
import CollapseIcon from "@/assets/table-bar/collapse.svg?component";

const props = {
  /** 头部最左边的标题 */
  title: {
    type: String,
    default: "列表"
  },
  /** 对于树形表格，如果想启用展开和折叠功能，传入当前表格的ref即可 */
  tableRef: {
    type: Object as PropType<any>
  },
  /** 需要展示的列 */
  columns: {
    type: Array as PropType<TableColumnList>,
    default: () => []
  },
  isExpandAll: {
    type: Boolean,
    default: true
  },
  tableKey: {
    type: [String, Number] as PropType<string | number>,
    default: "0"
  }
};

export default defineComponent({
  name: "PureTableBar",
  props,
  emits: ["refresh", "fullscreen"],
  setup(props, { emit, slots, attrs }) {
    const size = ref("default");
    const loading = ref(false);
    const checkAll = ref(true);
    const isFullscreen = ref(false);
    const isIndeterminate = ref(false);
    const instance = getCurrentInstance()!;
    const isExpandAll = ref(props.isExpandAll);
    const filterColumns = cloneDeep(props?.columns).filter(column =>
      isBoolean(column?.hide)
        ? !column.hide
        : !(isFunction(column?.hide) && column?.hide())
    );
    let checkColumnList = getKeyList(cloneDeep(props?.columns), "label");
    const checkedColumns = ref(getKeyList(cloneDeep(filterColumns), "label"));
    const dynamicColumns = ref(cloneDeep(props?.columns));

    const getDropdownItemStyle = computed(() => {
      return s => {
        return {
          background:
            s === size.value ? useEpThemeStoreHook().epThemeColor : "",
          color: s === size.value ? "#fff" : "var(--el-text-color-primary)"
        };
      };
    });

    const iconClass = computed(() => {
      return [
        "text-black",
        "dark:text-white",
        "duration-100",
        "hover:text-primary!",
        "cursor-pointer",
        "outline-hidden"
      ];
    });

    const topClass = computed(() => {
      return [
        "flex",
        "justify-between",
        "pt-[3px]",
        "px-[11px]",
        "border-b-[1px]",
        "border-solid",
        "border-[#dcdfe6]",
        "dark:border-[#303030]"
      ];
    });

    function onReFresh() {
      loading.value = true;
      emit("refresh");
      delay(500).then(() => (loading.value = false));
    }

    function onExpand() {
      isExpandAll.value = !isExpandAll.value;
      toggleRowExpansionAll(props.tableRef.data, isExpandAll.value);
    }

    function onFullscreen() {
      isFullscreen.value = !isFullscreen.value;
      emit("fullscreen", isFullscreen.value);
    }

    function toggleRowExpansionAll(data, isExpansion) {
      data.forEach(item => {
        props.tableRef.toggleRowExpansion(item, isExpansion);
        if (item.children !== undefined && item.children !== null) {
          toggleRowExpansionAll(item.children, isExpansion);
        }
      });
    }

    function handleCheckAllChange(val: CheckboxValueType) {
      checkedColumns.value = val ? checkColumnList : [];
      isIndeterminate.value = false;
      dynamicColumns.value.map(column =>
        val ? (column.hide = false) : (column.hide = true)
      );
    }

    function handleCheckedColumnsChange(value: CheckboxValueType[]) {
      checkedColumns.value = value;
      const checkedCount = value.length;
      checkAll.value = checkedCount === checkColumnList.length;
      isIndeterminate.value =
        checkedCount > 0 && checkedCount < checkColumnList.length;
    }

    function handleCheckColumnListChange(
      val: CheckboxValueType,
      label: string
    ) {
      dynamicColumns.value.filter(item => item.label === label)[0].hide = !val;
    }

    /** 列固定：`fixed` 传 `"left"` / `"right"` 固定到对应侧，传 `false` 取消固定 */
    function handleToggleColumnFixed(fixed, label: string) {
      const column = dynamicColumns.value.find(item => item.label === label);
      if (column) {
        column.fixed = fixed;
      }
    }

    async function onReset() {
      checkAll.value = true;
      isIndeterminate.value = false;
      dynamicColumns.value = cloneDeep(props?.columns);
      checkColumnList = [];
      checkColumnList = await getKeyList(cloneDeep(props?.columns), "label");
      checkedColumns.value = getKeyList(cloneDeep(filterColumns), "label");
    }

    const dropdown = {
      dropdown: () => (
        <el-dropdown-menu class="translation">
          <el-dropdown-item
            style={getDropdownItemStyle.value("large")}
            onClick={() => (size.value = "large")}
          >
            宽松
          </el-dropdown-item>
          <el-dropdown-item
            style={getDropdownItemStyle.value("default")}
            onClick={() => (size.value = "default")}
          >
            默认
          </el-dropdown-item>
          <el-dropdown-item
            style={getDropdownItemStyle.value("small")}
            onClick={() => (size.value = "small")}
          >
            紧凑
          </el-dropdown-item>
        </el-dropdown-menu>
      )
    };

    /** 列展示拖拽排序 */
    const rowDrop = (event: { preventDefault: () => void }) => {
      event.preventDefault();
      nextTick(() => {
        const wrapper: HTMLElement = (
          instance?.proxy?.$refs[`GroupRef${unref(props.tableKey)}`] as any
        ).$el.firstElementChild;
        Sortable.create(wrapper, {
          animation: 300,
          handle: ".drag-btn",
          onEnd: ({ newIndex, oldIndex, item }) => {
            const targetThElem = item;
            const wrapperElem = targetThElem.parentNode as HTMLElement;
            const oldColumn = dynamicColumns.value[oldIndex];
            const newColumn = dynamicColumns.value[newIndex];
            if (oldColumn?.fixed || newColumn?.fixed) {
              // 当前列存在fixed属性 则不可拖拽
              const oldThElem = wrapperElem.children[oldIndex] as HTMLElement;
              if (newIndex > oldIndex) {
                wrapperElem.insertBefore(targetThElem, oldThElem);
              } else {
                wrapperElem.insertBefore(
                  targetThElem,
                  oldThElem ? oldThElem.nextElementSibling : oldThElem
                );
              }
              return;
            }
            const currentRow = dynamicColumns.value.splice(oldIndex, 1)[0];
            dynamicColumns.value.splice(newIndex, 0, currentRow);
          }
        });
      });
    };

    /**
     * 返回某列的固定状态
     * 注：`element-plus` 里 `fixed: true` 等价于 `"left"`（见其 table store 的
     * `[true, "left"].includes(column.fixed)`），上游 bar 把 `true` 归到右侧，这里按
     * 组件库真实语义修正，避免页面声明 `fixed: true` 时高亮到错误的图钉
     */
    const isFixedColumn = (label: string) => {
      const column = dynamicColumns.value.find(item => item.label === label);
      const fixedOption = column?.fixed;
      const left = fixedOption === true || fixedOption === "left";
      const right = fixedOption === "right";
      return { fixed: left || right, left, right };
    };

    const rendTippyProps = (content: string) => {
      // https://vue-tippy.netlify.app/props
      return {
        content,
        offset: [0, 18],
        duration: [300, 0],
        followCursor: true,
        hideOnClick: "toggle"
      };
    };

    const reference = {
      reference: () => (
        <SettingIcon
          class={["w-[16px]", iconClass.value]}
          v-tippy={rendTippyProps("列设置")}
        />
      )
    };

    return () => (
      <>
        <div
          {...attrs}
          class={[
            "w-full",
            "px-2",
            "pb-2",
            "bg-bg_color",
            isFullscreen.value
              ? ["h-full!", "z-2002", "fixed", "inset-0"]
              : "mt-2"
          ]}
        >
          <div class="flex justify-between w-full h-[60px] p-4">
            {slots?.title ? (
              slots.title()
            ) : (
              <p class="font-bold truncate">{props.title}</p>
            )}
            <div class="flex items-center justify-around">
              {slots?.buttons ? (
                <div class="flex mr-4">{slots.buttons()}</div>
              ) : null}
              {props.tableRef?.size ? (
                <>
                  <ExpandIcon
                    class={["w-[16px]", iconClass.value]}
                    style={{
                      transform: isExpandAll.value ? "none" : "rotate(-90deg)"
                    }}
                    v-tippy={rendTippyProps(
                      isExpandAll.value ? "折叠" : "展开"
                    )}
                    onClick={() => onExpand()}
                  />
                  <el-divider direction="vertical" />
                </>
              ) : null}
              <RefreshIcon
                class={[
                  "w-[16px]",
                  iconClass.value,
                  loading.value ? "animate-spin" : ""
                ]}
                v-tippy={rendTippyProps("刷新")}
                onClick={() => onReFresh()}
              />
              <el-divider direction="vertical" />
              <el-dropdown
                v-slots={dropdown}
                trigger="click"
                v-tippy={rendTippyProps("密度")}
              >
                <CollapseIcon class={["w-[16px]", iconClass.value]} />
              </el-dropdown>
              <el-divider direction="vertical" />

              <el-popover
                v-slots={reference}
                placement="bottom-start"
                popper-style={{ padding: 0 }}
                width="245"
                trigger="click"
              >
                <div class={[topClass.value]}>
                  <el-checkbox
                    class="-mr-1!"
                    label="列展示"
                    v-model={checkAll.value}
                    indeterminate={isIndeterminate.value}
                    onChange={value => handleCheckAllChange(value)}
                  />
                  <el-button type="primary" link onClick={() => onReset()}>
                    重置
                  </el-button>
                </div>

                <div class="pt-[6px] pl-[11px]">
                  <el-scrollbar max-height="36vh">
                    <el-checkbox-group
                      ref={`GroupRef${unref(props.tableKey)}`}
                      modelValue={checkedColumns.value}
                      onChange={value => handleCheckedColumnsChange(value)}
                    >
                      <el-space
                        direction="vertical"
                        alignment="flex-start"
                        size={0}
                      >
                        {checkColumnList.map((item, index) => {
                          const { fixed, left, right } = isFixedColumn(item);
                          return (
                            <div class="flex items-center">
                              <DragIcon
                                class={[
                                  "drag-btn w-[16px] mr-2",
                                  fixed ? "cursor-no-drop!" : "cursor-grab!"
                                ]}
                                onMouseenter={(event: {
                                  preventDefault: () => void;
                                }) => rowDrop(event)}
                              />
                              <el-checkbox
                                key={index}
                                label={item}
                                value={item}
                                onChange={value =>
                                  handleCheckColumnListChange(value, item)
                                }
                              >
                                <span
                                  title={item}
                                  class="inline-block w-[120px] truncate hover:text-text_color_primary"
                                >
                                  {item}
                                </span>
                              </el-checkbox>
                              <iconifyIconOffline
                                class={[
                                  "ml-2",
                                  "w-[16px]",
                                  "h-[16px]",
                                  "hover:text-primary",
                                  "cursor-pointer",
                                  left ? "text-primary" : ""
                                ]}
                                icon={left ? PinAngleFill : PinAngle}
                                v-tippy={left ? "取消固定" : "固定在左边"}
                                onClick={() =>
                                  handleToggleColumnFixed(
                                    left ? false : "left",
                                    item
                                  )
                                }
                              />
                              <iconifyIconOffline
                                class={[
                                  "ml-2",
                                  "w-[16px]",
                                  "h-[16px]",
                                  "hover:text-primary",
                                  "scale-x-[-1]",
                                  "cursor-pointer",
                                  right ? "text-primary" : ""
                                ]}
                                icon={right ? PinAngleFill : PinAngle}
                                v-tippy={right ? "取消固定" : "固定在右边"}
                                onClick={() =>
                                  handleToggleColumnFixed(
                                    right ? false : "right",
                                    item
                                  )
                                }
                              />
                            </div>
                          );
                        })}
                      </el-space>
                    </el-checkbox-group>
                  </el-scrollbar>
                </div>
              </el-popover>
              <el-divider direction="vertical" />

              <iconifyIconOffline
                class={["w-[16px]", iconClass.value]}
                icon={isFullscreen.value ? ExitFullscreen : Fullscreen}
                v-tippy={isFullscreen.value ? "退出全屏" : "全屏"}
                onClick={() => onFullscreen()}
              />
            </div>
          </div>
          {slots.default({
            size: size.value,
            dynamicColumns: dynamicColumns.value
          })}
        </div>
      </>
    );
  }
});
